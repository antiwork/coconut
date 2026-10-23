"""Snapshot local review images into the authenticated artifact store."""
import os
from pathlib import Path
import stat
from io import BytesIO
from uuid import uuid4

MAX_IMAGE_BYTES = 4 * 1024 * 1024


def image_size(content):
    # Pillow ships with Hermes. Decode before pausing for a decision.
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(content), formats=['PNG', 'JPEG']) as image:
            width, height = image.size
            if min(width, height) < 1 or max(width, height) > 16000 or width * height > 40_000_000:
                raise ValueError('Keep preview dimensions under 16,000 pixels and 40 megapixels.')
            extension = 'png' if image.format == 'PNG' else 'jpg'
            image.verify()
        with Image.open(BytesIO(content), formats=['PNG', 'JPEG']) as image:
            image.load()
        return width, height, extension
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError('Use a complete PNG or JPEG image that can be opened.') from error


def attach_previews(service, decision, payload):
    from .service import ServiceError, digest, encoded, now
    if 'previews' not in payload:
        return
    previews = payload['previews']
    if not isinstance(previews, list) or len(previews) > 6:
        raise ServiceError(400, 'Attach up to six preview images.')
    prepared = []
    total = 0
    for item in previews:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('caption'), str):
            raise ServiceError(400, 'Each preview needs an absolute path and a caption.')
        path, caption = Path(item['path']), item['caption'].strip()
        if not path.is_absolute() or not caption or len(caption) > 160:
            raise ServiceError(400, 'Use an absolute image path and a short caption.')
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
                    raise ValueError('Each preview must be a regular image file under 4 MB.')
                content = source.read(MAX_IMAGE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_IMAGE_BYTES or total > 12 * 1024 * 1024:
                raise ValueError('Keep each image under 4 MB and all previews under 12 MB.')
            width, height, extension = image_size(content)
        except (OSError, ValueError) as error:
            raise ServiceError(400, str(error) if isinstance(error, ValueError) else 'The preview image could not be read.') from error
        artifact_id = str(uuid4())
        prepared.append((dict(artifact_id=artifact_id, caption=caption, width=width, height=height),
            dict(id=artifact_id, bot_id=decision['bot_id'], task_id=decision['task_id'],
                 title=f'preview.{extension}', kind='file', status='ready', preview=caption, url=None,
                 created_at=now(), _hidden=True, _decision_id=decision['id']), content))
    # Validate the whole set before writing any bytes. The caller owns the transaction.
    for _, artifact, content in prepared:
        service._put('artifacts', artifact)
        service.store_artifact_content(artifact['id'], content)
    decision['previews'] = [preview for preview, _, _ in prepared]
    decision['previews_hash'] = digest(encoded(decision['previews'])) if prepared else None
