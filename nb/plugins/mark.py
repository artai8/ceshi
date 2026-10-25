import logging  
import os  
import shutil  
from typing import Any, Dict, List  
  
import requests  
from pydantic import BaseModel  
from watermark import File, Position, Watermark, apply_watermark  
  
from nb.plugin_models import MarkConfig  
from nb.plugins import NbMessage, NbPlugin  
from nb.multitenant.media_pool import run_cpu
from nb.utils import cleanup  


def _watermark_file(base_path: str, overlay_path: str, position, frame_rate: int) -> str:
    base = File(base_path)
    overlay = File(overlay_path)
    watermark = Watermark(overlay, position)
    return apply_watermark(base, watermark, frame_rate=frame_rate)
  
  
def download_image(url: str, filename: str = "image.png") -> bool:  
    if filename in os.listdir():  
        logging.info("Image for watermarking already exists.")  
        return True  
    try:  
        logging.info(f"Downloading image {url}")  
        response = requests.get(url, stream=True)  
        if response.status_code == 200:  
            logging.info("Got Response 200")  
            with open(filename, "wb") as file:  
                response.raw.decode_content = True  
                shutil.copyfileobj(response.raw, file)  
    except Exception as err:  
        logging.error(err)  
        return False  
    else:  
        logging.info("File created image")  
        return True  
  
  
class NbMark(NbPlugin):  
    id_ = "mark"  
  
    def __init__(self, data) -> None:  
        self.data = data  
  
    async def modify(self, tm: NbMessage) -> NbMessage:  
        if not tm.file_type in ["gif", "video", "photo"]:  
            return tm  
        downloaded_file = await tm.get_file()  
        if self.data.image.startswith("https://"):  
            download_image(self.data.image)  
            overlay_path = "image.png"
        else:  
            overlay_path = self.data.image
        tm.new_file = await run_cpu(
            _watermark_file,
            downloaded_file,
            overlay_path,
            self.data.position,
            self.data.frame_rate,
        )
        cleanup(downloaded_file)  
        tm.cleanup = True  
        return tm  
  
    async def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:  
        """Apply watermark to each media message in the group."""  
        for tm in tms:  
            if tm.file_type in ["gif", "video", "photo"]:  
                await self.modify(tm)  
        return tms
