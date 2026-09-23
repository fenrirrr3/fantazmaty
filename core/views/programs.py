import logging
from pathlib import Path
from zipfile import BadZipFile

from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from docx.oxml.exceptions import InvalidXmlError
from lxml.etree import XMLSyntaxError

from core.odkurzacz_forms import OdkurzaczForm
from core.permissions import team_member_required
from core.services.odkurzacz import clean_docx

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["GET", "POST"])
@team_member_required
def programs(request):
    form = OdkurzaczForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["document"]
        try:
            output = clean_docx(upload, form.cleaned_data["rules"])
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
    return render(request, "core/programs.html", {"form": form})
