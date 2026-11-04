"""Authenticated, bounded download of inbound WhatsApp media."""
from io import BytesIO
import os
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader

from app.api.whatsapp import graph_api_url, whatsapp_graph_base_url, DEFAULT_WHATSAPP_GRAPH_API_VERSION
from app.services.media_contracts import InboundMedia, ValidatedMedia

MAX_MEDIA_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MIME_TYPES = {
    'audio': {'audio/ogg', 'audio/mpeg', 'audio/mp3'},
    'image': {'image/jpeg', 'image/png'},
    'document': {'application/pdf'},
}


class MediaError(ValueError):
    pass


def _safe_media_url(url: str) -> bool:
    parsed = urlsplit(url)
    configured = urlsplit(whatsapp_graph_base_url())
    if parsed.username or parsed.password:
        return False
    if parsed.scheme == 'http':
        return configured.scheme == 'http' and parsed.netloc == configured.netloc
    if parsed.scheme != 'https' or parsed.port not in {None, 443}:
        return False
    host = (parsed.hostname or '').lower()
    allowed = ('facebook.com', 'fbcdn.net', 'fbsbx.com')
    if any(host == suffix or host.endswith('.' + suffix) for suffix in allowed):
        return True
    return False


def _validate_content(kind: str, mime: str, content: bytes) -> None:
    if not content or len(content) > MAX_MEDIA_BYTES:
        raise MediaError('El archivo está vacío o supera 5 MiB.')
    signatures = {
        'audio/ogg': lambda raw: raw.startswith(b'OggS'),
        'audio/mpeg': lambda raw: raw.startswith(b'ID3') or raw[:2] in {b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'},
        'audio/mp3': lambda raw: raw.startswith(b'ID3') or raw[:2] in {b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'},
        'image/jpeg': lambda raw: raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9'),
        'image/png': lambda raw: raw.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in raw[-16:],
        'application/pdf': lambda raw: raw.startswith(b'%PDF-'),
    }
    if not signatures[mime](content):
        raise MediaError('El contenido del archivo no coincide con su formato.')
    if kind == 'document':
        try:
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
                raise MediaError('El PDF está protegido o supera 20 páginas.')
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError('El PDF no se puede leer.') from exc


async def download_media(media: InboundMedia) -> ValidatedMedia:
    token = os.getenv('WHATSAPP_API_TOKEN')
    if not token:
        raise MediaError('La descarga de adjuntos no está configurada.')
    version = os.getenv('WHATSAPP_GRAPH_API_VERSION', DEFAULT_WHATSAPP_GRAPH_API_VERSION)
    if not version.startswith('v') or not version[1:].replace('.', '').isdigit():
        raise MediaError('La configuración de WhatsApp no es válida.')
    headers = {'Authorization': f'Bearer {token}'}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        try:
            phone_id = os.getenv('WHATSAPP_PHONE_ID')
            meta_response = await client.get(graph_api_url(f'{version}/{media.media_id}'), headers=headers,
                                             params={'phone_number_id': phone_id} if phone_id else None)
            meta_response.raise_for_status()
            metadata = meta_response.json()
            mime = str(metadata.get('mime_type') or '').split(';', 1)[0].strip().lower()
            if mime not in MIME_TYPES[media.kind]:
                raise MediaError('Formato de adjunto no admitido.')
            if media.mime_type and media.mime_type.split(';', 1)[0].strip().lower() != mime:
                raise MediaError('El formato del adjunto no coincide con el informado por WhatsApp.')
            if int(metadata.get('file_size', 0)) > MAX_MEDIA_BYTES:
                raise MediaError('El archivo supera 5 MiB.')
            url = metadata.get('url')
            if not isinstance(url, str) or not _safe_media_url(url):
                raise MediaError('La dirección de descarga no es válida.')
            content = bytearray()
            async with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                if int(response.headers.get('content-length', 0)) > MAX_MEDIA_BYTES:
                    raise MediaError('El archivo supera 5 MiB.')
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > MAX_MEDIA_BYTES:
                        raise MediaError('El archivo supera 5 MiB.')
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, MediaError):
                raise
            raise MediaError('No pude descargar el adjunto de WhatsApp.') from exc
    _validate_content(media.kind, mime, bytes(content))
    return ValidatedMedia(media.kind, mime, bytes(content), media.caption)
