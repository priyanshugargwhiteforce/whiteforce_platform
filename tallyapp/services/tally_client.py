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
            "ngrok-skip-browser-warning": "true",
        }

        for attempt in range(retries):
            try:
                response = requests.post(
                    self.url,
                    data=xml.encode("utf-8"),
                    headers=headers,
                    timeout=10,  # fail fast while debugging; raise to 60 once stable
                )
                response.raise_for_status()
                data = response.content.decode("utf-8", errors="ignore")
                if "<ENVELOPE" in data:
                    return data
                print(f"[TallyClient] attempt {attempt+1}: got 200 but no <ENVELOPE> "
                      f"in response. url={self.url} first_300_chars={data[:300]!r}")
            except Exception as e:
                print(f"[TallyClient] attempt {attempt+1} FAILED for url={self.url}: "
                      f"{type(e).__name__}: {e}")
                time.sleep(1)

        return "<ERROR>Tally request failed</ERROR>"