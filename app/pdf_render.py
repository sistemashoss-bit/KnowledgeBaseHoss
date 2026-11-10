"""HTML del editor de documentos (doceditor/) → PDF con WeasyPrint.

El front manda el documento HTML completo (con su @page A4), el mismo que
imprime en el navegador. Aquí no se descarga nada: solo se aceptan recursos
data: (las imágenes del editor van embebidas), para que el HTML de un usuario
no pueda hacer que el servidor pida URLs internas o lea archivos locales.
"""
from weasyprint import HTML
from weasyprint.urls import URLFetcher


def html_to_pdf(html: str) -> bytes:
    fetcher = URLFetcher(allowed_protocols={"data"}, allow_redirects=False)
    return HTML(string=html, base_url=None, url_fetcher=fetcher).write_pdf()
