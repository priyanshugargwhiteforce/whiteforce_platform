from .tally_client import TallyClient
from .xml_builder import get_ledger_xml
from .xml_parser import parse_ledgers


class LedgerService:

    def __init__(self, client: TallyClient):
        self.client = client

    def get_all(self):
        xml      = get_ledger_xml()
        response = self.client.send(xml)
        return parse_ledgers(response)