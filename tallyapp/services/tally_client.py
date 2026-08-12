import requests
import time


class TallyClient:

    def __init__(self, url: str):
        self.url = url

    def send(self, xml: str, retries: int = 3) -> str:

        headers = {
            "Content-Type": "text/xml",
            "Connection":   "close",
            "Accept":       "text/xml",
        }

        for attempt in range(retries):
            try:
                response = requests.post(
                    self.url,
                    data=xml.encode("utf-8"),
                    headers=headers,
                    timeout=60,
                )
                response.raise_for_status()
                data = response.content.decode("utf-8", errors="ignore")
                if "<ENVELOPE" in data:
                    return data
            except Exception:
                time.sleep(1)

        return "<ERROR>Tally request failed</ERROR>"