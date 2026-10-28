import requests
import time


class TallyUnavailable(Exception):
    """Raised when Tally can't be reached or returns an unusable response."""


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

        last_error = None

        for attempt in range(retries):
            try:
                response = requests.post(
                    self.url,
                    data=xml.encode("utf-8"),
                    headers=headers,
                    timeout=(10, 60),  # (connect, read)
                )
                response.raise_for_status()
                data = response.content.decode("utf-8", errors="ignore")
                if "<ENVELOPE" in data:
                    return data
                last_error = f"got {response.status_code} but no <ENVELOPE> in response: {data[:300]!r}"
            except requests.ReadTimeout as e:
                # Tally is single-threaded and still busy with this query —
                # re-sending it would only queue another copy behind it.
                raise TallyUnavailable(f"Tally did not respond in time ({self.url}): {e}")
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"

            print(f"[TallyClient] attempt {attempt+1} FAILED for url={self.url}: {last_error}")
            if attempt < retries - 1:
                time.sleep(1)

        raise TallyUnavailable(f"Tally request failed ({self.url}): {last_error}")
