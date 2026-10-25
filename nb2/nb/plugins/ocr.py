import pytesseract
from PIL import Image

from nb.plugins import NbMessage, NbPlugin
from nb.multitenant.media_pool import run_cpu
from nb.utils import cleanup


def _ocr_file(path: str, lang: str) -> str:
    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang=lang)


class NbOcr(NbPlugin):
    id_ = "ocr"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:

        if not tm.file_type in ["photo"]:
            return tm

        file = await tm.get_file()
        lang = getattr(self.data, "lang", "chi_sim")
        try:
            tm.text = await run_cpu(_ocr_file, file, lang)
        finally:
            cleanup(file)
        return tm
