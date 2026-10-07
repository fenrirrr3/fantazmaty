import logging
from pathlib import Path
from zipfile import BadZipFile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from docx.oxml.exceptions import InvalidXmlError
from lxml.etree import XMLSyntaxError

from core.odkurzacz_forms import OdkurzaczForm, DocumentConversionForm, RepetitionsForm
from core.services.document_converter import convert_document, conversion_filename, ConversionError, RebuildConfirmationRequired
from core.permissions import team_member_required

from core.services.pending_documents import save_pending, load_pending, PendingDocumentError

logger = logging.getLogger(__name__)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def programs(request):
    rebuild_warning = False
    rebuild_token = ""
    action = request.POST.get('program_action', 'clean') if request.method == 'POST' else None
    pending_path = None
    continuation_error = ''
    form_data = request.POST if action == 'clean' else None
    form_files = request.FILES if action == 'clean' else None
    accepted = False
    if action == 'clean_confirm':
        try:
            upload, pending, pending_path = load_pending(request, request.POST.get('rebuild_token', ''))
        except PendingDocumentError as error:
            continuation_error = str(error)
        else:
            form_data = pending['options']
            form_files = {'document': upload}
            rebuild_token = request.POST['rebuild_token']
            rebuild_warning = pending['warning']
            accepted = True
    form = OdkurzaczForm(form_data, form_files)
    conversion_form = DocumentConversionForm(
        request.POST if action == 'convert' else None,
        request.FILES if action == 'convert' else None, prefix='convert',
    )
    repetitions_form = RepetitionsForm(
        request.POST if action == 'repetitions' else None,
        request.FILES if action == 'repetitions' else None, prefix='repetitions',
    )
    if action == 'repetitions' and repetitions_form.is_valid():
        upload = repetitions_form.cleaned_data['document']
        try:
            output, _, _ = convert_document(upload, [], include_docx=True, normalize=False,
                                            repetitions=repetitions_form.analysis_config())
        except ConversionError as error:
            repetitions_form.add_error(None, str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, ValueError, OSError):
            repetitions_form.add_error('document', 'Nie można przeanalizować dokumentu. Sprawdź plik i zaakceptuj śledzone zmiany. Limit: 500 000 znaków i 20 000 znaków w akapicie.')
        except Exception:
            logger.exception('Błąd analizy powtórzeń')
            repetitions_form.add_error(None, 'Analiza nie powiodła się. Skontaktuj się z administratorem.')
        else:
            response = FileResponse(output, as_attachment=True,
                filename=Path(upload.name).stem[:120] + '_powtorzenia.docx',
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            if getattr(output, 'conversion_warnings', []):
                messages.warning(request, 'Konwersja zgłosiła uproszczenia dokumentu. W paczce ZIP szczegóły są w pliku Uwagi_konwersji.txt; sprawdź plik wynikowy.')
                response['X-Document-Warning-Count'] = str(len(output.conversion_warnings))
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    if action == 'convert' and conversion_form.is_valid():
        upload = conversion_form.cleaned_data['document']
        try:
            output, extension, mime = convert_document(upload,
                conversion_form.cleaned_data['formats'],
                use_cleaner=conversion_form.cleaned_data['use_cleaner'], preserve_filename=True, remove_soft_whitespace=conversion_form.cleaned_data['remove_soft_whitespace'])
        except ConversionError as error:
            conversion_form.add_error(None, str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, KeyError, ValueError, OSError):
            conversion_form.add_error('document', 'Nie udało się przetworzyć DOCX. Sprawdź plik lub zapisz dokument ponownie jako DOCX.')
        except Exception:
            logger.exception('Błąd konwersji dokumentu')
            conversion_form.add_error(None, 'Konwersja nie powiodła się. Skontaktuj się z administratorem.')
        else:
            response = FileResponse(output, as_attachment=True,
                filename=conversion_filename(upload.name, extension),
                content_type=mime)
            if getattr(output, 'conversion_warnings', []):
                messages.warning(request, 'Konwersja zgłosiła uproszczenia dokumentu. W paczce ZIP szczegóły są w pliku Uwagi_konwersji.txt; sprawdź plik wynikowy.')
                response['X-Document-Warning-Count'] = str(len(output.conversion_warnings))
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    if action in ('clean', 'clean_confirm') and not continuation_error and form.is_valid():
        upload = form.cleaned_data['document']
        rebuild = form.cleaned_data['rebuild']
        try:
            output, extension, mime = convert_document(upload, [], include_docx=True,
                rebuild=rebuild, normalize=form.cleaned_data['normalize_formatting'],
                justify=form.cleaned_data['normalize_formatting'], allow_rebuild_omissions=accepted,
                use_cleaner=True, cleaner_rules=form.cleaned_data['rules'],
                remove_soft_whitespace=form.cleaned_data['remove_soft_whitespace'])
        except RebuildConfirmationRequired as error:
            rebuild_warning = str(error)
            try:
                rebuild_token = save_pending(request, upload,
                    {key: form.cleaned_data[key] for key in ('rebuild', 'normalize_formatting', 'rules', 'remove_soft_whitespace')},
                    rebuild_warning)
            except (OSError, PendingDocumentError):
                logger.exception('Nie udało się zachować dokumentu do potwierdzenia')
                rebuild_warning = False
                form.add_error(None, 'Nie udało się zachować pliku do potwierdzenia. Spróbuj przesłać dokument ponownie.')
        except ConversionError as error:
            form.add_error(None, str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, KeyError, ValueError, OSError):
            form.add_error(None, 'Nie udało się przetworzyć dokumentu. Sprawdź plik lub zapisz go ponownie jako DOCX.')
        except Exception:
            logger.exception('Błąd przetwarzania dokumentu w Odkurzaczu')
            form.add_error(None, 'Wystąpił błąd przetwarzania. Spróbuj ponownie lub skontaktuj się z administratorem.')
        else:
            if pending_path is not None:
                pending_path.unlink(missing_ok=True)
            suffix = '_nowy' if rebuild else '_odkurzony'
            response = FileResponse(output, as_attachment=True,
                filename=Path(upload.name).stem[:120] + suffix + '.' + extension, content_type=mime)
            if getattr(output, 'conversion_warnings', []):
                response['X-Document-Warning-Count'] = str(len(output.conversion_warnings))
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    if action not in (None, 'clean', 'clean_confirm', 'convert', 'repetitions'):
        form = OdkurzaczForm(request.POST, request.FILES)
        form.add_error(None, 'Wybierz narzędzie i wyślij formularz ponownie.')
    return render(request, "core/programs.html", {
        "form": form, "conversion_form": conversion_form, "repetitions_form": repetitions_form,
        "repetitions_open": action == "repetitions",
        'rebuild_token': rebuild_token, 'rebuild_warning': rebuild_warning, 'rebuild_warning_text': rebuild_warning,
        'continuation_error': continuation_error, 'clean_open': bool(form.errors or rebuild_warning or continuation_error), "conversion_open": action == 'convert',
    })
