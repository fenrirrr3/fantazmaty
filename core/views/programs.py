import logging
import hashlib
from django.core import signing
from pathlib import Path
from zipfile import BadZipFile

from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from docx.oxml.exceptions import InvalidXmlError
from lxml.etree import XMLSyntaxError

from core.odkurzacz_forms import OdkurzaczForm, DocumentConversionForm
from core.services.document_converter import convert_document, ConversionError, RebuildConfirmationRequired, REBUILD_WARNING
from core.permissions import team_member_required
from core.services.odkurzacz import clean_docx

logger = logging.getLogger(__name__)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def programs(request):
    rebuild_warning = False
    rebuild_token = ""
    action = request.POST.get('program_action', 'clean') if request.method == 'POST' else None
    form = OdkurzaczForm(
        request.POST if action == 'clean' else None,
        request.FILES if action == 'clean' else None,
    )
    conversion_form = DocumentConversionForm(
        request.POST if action == 'convert' else None,
        request.FILES if action == 'convert' else None, prefix='convert',
    )
    if action == 'convert' and conversion_form.is_valid():
        upload = conversion_form.cleaned_data['document']
        try:
            output, extension, mime = convert_document(upload,
                conversion_form.cleaned_data['formats'],
                use_cleaner=conversion_form.cleaned_data['use_cleaner'])
        except ConversionError as error:
            conversion_form.add_error(None, str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, KeyError, ValueError, OSError):
            conversion_form.add_error('document', 'Nie udało się przetworzyć DOCX. Zapisz dokument ponownie; przy użyciu Odkurzacza obowiązuje limit 500 000 znaków i 20 000 znaków w akapicie.')
        except Exception:
            logger.exception('Błąd konwersji dokumentu')
            conversion_form.add_error(None, 'Konwersja nie powiodła się. Skontaktuj się z administratorem.')
        else:
            response = FileResponse(output, as_attachment=True,
                filename=Path(upload.name).stem[:120] + '_konwersja.' + extension,
                content_type=mime)
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    if action == 'clean' and form.is_valid() and form.cleaned_data['rebuild']:
        upload = form.cleaned_data['document']
        digest = hashlib.sha256(upload.read()).hexdigest()
        upload.seek(0)
        accepted = False
        if request.POST.get('allow_rebuild_omissions') == 'on':
            try:
                approved = signing.loads(request.POST.get('rebuild_token', ''), salt='rebuild-omissions', max_age=3600)
                accepted = approved == {'user': request.user.pk, 'digest': digest}
            except signing.BadSignature:
                pass
        try:
            output, _, _ = convert_document(upload, [], include_docx=True, rebuild=True, normalize=False, allow_rebuild_omissions=accepted, use_cleaner=True, cleaner_rules=form.cleaned_data['rules'])
        except RebuildConfirmationRequired as error:
            rebuild_warning = str(error)
            rebuild_token = signing.dumps({'user': request.user.pk, 'digest': digest}, salt='rebuild-omissions')
            form.add_error('document', str(error))
        except ConversionError as error:
            form.add_error('document', str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, ValueError, OSError):
            form.add_error('document', 'Nie można odczytać dokumentu. Zapisz go ponownie jako DOCX.')
        else:
            response = FileResponse(output, as_attachment=True,
                filename=Path(upload.name).stem[:120] + '_nowy.docx',
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    if action == 'clean' and form.is_valid() and not form.cleaned_data['rebuild']:
        upload = form.cleaned_data["document"]
        try:
            output, _, _ = convert_document(upload, [], include_docx=True, use_cleaner=True, normalize=False, cleaner_rules=form.cleaned_data["rules"])
        except ConversionError as error:
            form.add_error("document", str(error))
        except (BadZipFile, XMLSyntaxError, InvalidXmlError, KeyError, ValueError, OSError):
            form.add_error("document", "Nie udało się przetworzyć dokumentu. Plik może być uszkodzony lub zbyt rozbudowany. Otwórz go w Wordzie i zapisz ponownie jako DOCX; bardzo długi tekst podziel na części.")
        except Exception:
            logger.exception("Błąd przetwarzania dokumentu w Odkurzaczu")
            form.add_error(None, "Wystąpił błąd przetwarzania. Spróbuj ponownie lub skontaktuj się z administratorem.")
        else:
            response = FileResponse(
                output, as_attachment=True,
                filename=Path(upload.name).stem[:150] + "_odkurzony.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
    if action not in (None, 'clean', 'convert'):
        form = OdkurzaczForm(request.POST, request.FILES)
        form.add_error(None, 'Wybierz narzędzie i wyślij formularz ponownie.')
    return render(request, "core/programs.html", {
        "form": form, "conversion_form": conversion_form,
        'rebuild_token': rebuild_token, 'rebuild_warning': rebuild_warning, 'rebuild_warning_text': rebuild_warning,
        'clean_open': bool(form.errors), "conversion_open": action == 'convert',
    })
