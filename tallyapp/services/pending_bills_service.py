import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

from .tally_client import TallyClient


class PendingBillsService:
    """
    Production-ready Pending Bills Service.

    Fetches:
        - Bills Receivable
        - Opening Balance
        - Merges both datasets

    Returns:

        Party Name
        Bill Reference
        Bill Date
        Due Date
        Opening Balance
        Closing Amount
        Overdue Days
    """

    def __init__(self, client: TallyClient):
        self.client = client

    ###########################################################################
    # XML REQUESTS
    ###########################################################################

    BILLS_XML = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Bills Receivable</ID>
    </HEADER>

    <BODY>

        <DESC>

            <STATICVARIABLES>

                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>

            </STATICVARIABLES>

        </DESC>

    </BODY>

</ENVELOPE>
"""

    OPENING_XML = """
<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>OpeningBalanceCollection</ID>
 </HEADER>

 <BODY>

  <DESC>

   <STATICVARIABLES>

    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>

   </STATICVARIABLES>

   <TDL>

    <TDLMESSAGE>

     <COLLECTION NAME="OpeningBalanceCollection">

      <TYPE>Ledger</TYPE>

      <CHILDOF>$$GroupSundryDebtors</CHILDOF>

      <BELONGSTO>Yes</BELONGSTO>

      <FETCH>

        NAME,

        BILLALLOCATIONS.NAME,

        BILLALLOCATIONS.BILLDATE,

        BILLALLOCATIONS.BILLCREDITPERIOD,

        BILLALLOCATIONS.OPENINGBALANCE

      </FETCH>

     </COLLECTION>

    </TDLMESSAGE>

   </TDL>

  </DESC>

 </BODY>

</ENVELOPE>
"""

    # Reliable way to get the company currently loaded on the Tally XML
    # server. Unlike requesting a report and hoping SVCURRENTCOMPANY shows
    # up in the response (it doesn't - that's a request-side static
    # variable, not part of the export payload), this pulls the company
    # directly from a Company collection, which Tally always populates.
    COMPANY_XML = """
<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>CompanyCollection</ID>
 </HEADER>

 <BODY>

  <DESC>

   <STATICVARIABLES>

    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>

   </STATICVARIABLES>

   <TDL>

    <TDLMESSAGE>

     <COLLECTION NAME="CompanyCollection" ISINITIALIZE="Yes">

      <TYPE>Company</TYPE>

      <FETCH>NAME</FETCH>

     </COLLECTION>

    </TDLMESSAGE>

   </TDL>

  </DESC>

 </BODY>

</ENVELOPE>
"""

    ###########################################################################
    # HTTP REQUESTS
    ###########################################################################

    def _fetch_bills_xml(self):

        return self.client.send(self.BILLS_XML)

    def _fetch_opening_balance_xml(self):

        response = requests.post(
            self.client.url,
            data=self.OPENING_XML.encode("utf-8"),
            headers={
                "Content-Type": "text/xml",
                "Accept": "text/xml",
            },
            timeout=60,
        )

        response.raise_for_status()

        return response.text

    ###########################################################################
    # COMPANY NAME
    ###########################################################################

    def get_company_name(self):
        """
        Fetch the name of the company currently loaded on the Tally
        XML server via a Company collection.

        Previous implementation requested the "Day Book" report and
        searched the response for an <SVCURRENTCOMPANY> node - but that
        tag is a request-side STATICVARIABLE, Tally does not echo it
        back in the export payload, so this always returned "".
        """

        data = self.client.send(self.COMPANY_XML)

        try:

            root = ET.fromstring(data)

            # Company collection returns one or more <COMPANY NAME="..."/>
            # elements. In a normal single-company XML server session
            # there will be exactly one.
            node = root.find(".//COMPANY")

            if node is not None:

                name = node.attrib.get("NAME")

                if name:
                    return self.clean(name)

                # Some Tally versions nest it as a child <NAME> element
                # instead of an attribute.
                name_child = node.find("NAME")

                if name_child is not None and name_child.text:
                    return self.clean(name_child.text)

        except Exception as e:

            print("get_company_name() failed to parse response:", e)

        return ""

    ###########################################################################
    # UTILITIES
    ###########################################################################

    @staticmethod
    def clean(value):

        if value is None:
            return ""

        value = html.unescape(value)

        value = value.replace("\r", " ")

        value = value.replace("\n", " ")

        value = " ".join(value.split())

        return value.strip()

    @staticmethod
    def xml_text(node, tag):

        if node is None:
            return ""

        child = node.find(tag)

        if child is None:
            return ""

        if child.text is None:
            return ""

        return child.text.strip()

    @staticmethod
    def format_date(date_text):

        if not date_text:

            return ""

        try:

            return datetime.strptime(
                date_text,
                "%Y%m%d"
            ).strftime("%d-%b-%y")

        except Exception:

            return date_text

    ###########################################################################
    # NORMALIZATION HELPERS
    ###########################################################################

    @staticmethod
    def normalize_party(value):
        """
        Normalize Party Name for use as a matching key: unescape HTML
        entities, collapse whitespace, and casefold so that matching is
        insensitive to case and formatting differences between the two
        Tally exports (Bills Receivable vs Opening Balance Collection).
        """

        if value is None:
            return ""

        value = html.unescape(value)

        value = value.replace("\r", " ")

        value = value.replace("\n", " ")

        value = " ".join(value.split())

        return value.casefold()

    @staticmethod
    def normalize_bill_ref(value):
        """
        Normalize Bill Reference for use as a matching key.
        """

        if value is None:
            return ""

        value = html.unescape(value)

        value = value.replace("\r", " ")

        value = value.replace("\n", " ")

        value = " ".join(value.split())

        return value.casefold()

    @staticmethod
    def normalize_amount(value):
        """
        Always return amount with 2 decimal places.
        """

        if value in ("", None):

            return ""

        try:

            return "{:.2f}".format(float(value))

        except Exception:

            return value

    ###########################################################################
    # BILLS RECEIVABLE PARSER
    ###########################################################################

    def _parse_bills_receivable(self):
        """
        Parse Bills Receivable XML using ElementTree.

        Returns:
            List[dict]
        """

        xml = self._fetch_bills_xml()

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise Exception(f"Invalid Bills Receivable XML: {e}")

        bills = []

        #
        # Every BILLFIXED corresponds to one pending bill.
        #
        for bill in root.iter("BILLFIXED"):

            party = self.clean(
                self.xml_text(bill, "BILLPARTY")
            )

            bill_ref = self.clean(
                self.xml_text(bill, "BILLREF")
            )

            bill_date = self.format_date(
                self.xml_text(bill, "BILLDATE")
            )

            bills.append(
                {
                    "party": party,
                    "bill_ref": bill_ref,
                    "bill_date": bill_date,
                    "closing_amount": "",
                    "due_date": "",
                    "overdue_days": "",
                    "opening_balance": None,
                }
            )

        #
        # Closing Amount
        #
        closing_values = [
            self.clean(node.text)
            for node in root.iter("BILLCL")
        ]

        #
        # Due Date
        #
        due_values = [
            self.clean(node.text)
            for node in root.iter("BILLDUE")
        ]

        #
        # Overdue Days
        #
        overdue_values = [
            self.clean(node.text)
            for node in root.iter("BILLOVERDUE")
        ]

        #
        # Merge values by index.
        #
        # NOTE: this assumes BILLCL / BILLDUE / BILLOVERDUE appear in the
        # XML in the same order and count as BILLFIXED. That assumption
        # holds for a well-formed Tally export, but if it doesn't, silently
        # truncating with min() would misalign data across every bill
        # after the mismatch point rather than failing loudly. We now
        # warn when the counts disagree so a mismatch is visible instead
        # of silently corrupting the data.
        #
        counts = {
            "bills": len(bills),
            "closing": len(closing_values),
            "due": len(due_values),
            "overdue": len(overdue_values),
        }

        if len(set(counts.values())) != 1:
            print("=" * 80)
            print("WARNING: Bills Receivable field counts do not match - "
                  "positional merge may misalign data.")
            print(counts)
            print("=" * 80)

        total = min(counts.values())

        for i in range(total):

            bills[i]["closing_amount"] = self.normalize_amount(
                closing_values[i]
            )

            bills[i]["due_date"] = due_values[i]

            bills[i]["overdue_days"] = overdue_values[i]

        return bills

    ###########################################################################
    # OPENING BALANCE PARSER
    ###########################################################################

    def _parse_opening_balances(self):
        """
        Parse Opening Balance XML.

        XML Structure:

        <LEDGER NAME="">
            <BILLALLOCATIONS.LIST>
                <NAME>Bill Ref</NAME>
                <BILLDATE>20220401</BILLDATE>
                <BILLCREDITPERIOD>30 Days</BILLCREDITPERIOD>
                <OPENINGBALANCE>-5000</OPENINGBALANCE>
            </BILLALLOCATIONS.LIST>
        </LEDGER>

        Returns
        -------
        lookup[(normalized_party, normalized_bill_ref)] = {
            party,           # original display value
            bill_ref,        # original display value
            opening_balance,
            bill_date,
            credit_period
        }
        """

        xml = self._fetch_opening_balance_xml()

        try:
            root = ET.fromstring(xml)

        except ET.ParseError as e:

            raise Exception(f"Invalid Opening Balance XML : {e}")

        lookup = {}

        total_ledgers = 0
        total_bill_allocations = 0

        #######################################################################
        # Iterate Every Ledger
        #######################################################################

        for ledger in root.iter("LEDGER"):

            total_ledgers += 1

            party = self.clean(
                ledger.attrib.get("NAME", "")
            )

            if not party:
                continue

            ###################################################################
            # Iterate Bill Allocations
            ###################################################################

            for bill in ledger.findall("BILLALLOCATIONS.LIST"):

                total_bill_allocations += 1

                bill_ref = self.clean(
                    self.xml_text(bill, "NAME")
                )

                bill_date = self.format_date(
                    self.xml_text(bill, "BILLDATE")
                )

                credit_period = self.clean(
                    self.xml_text(bill, "BILLCREDITPERIOD")
                )

                opening_balance = self.normalize_amount(
                    self.clean(self.xml_text(bill, "OPENINGBALANCE"))
                )

                if not bill_ref:
                    continue

                # KEY FIX: use normalized (casefolded, whitespace-collapsed)
                # party/bill_ref as the lookup key so that matching in
                # _merge_bills is not sensitive to case or stray
                # whitespace/HTML-entity differences between this export
                # and the Bills Receivable export.
                key = (
                    self.normalize_party(party),
                    self.normalize_bill_ref(bill_ref),
                )

                lookup[key] = {
                    "party": party,
                    "bill_ref": bill_ref,
                    "opening_balance": opening_balance,
                    "bill_date": bill_date,
                    "credit_period": credit_period,
                }

        #######################################################################
        # Debug Information
        #######################################################################

        print("=" * 80)
        print("Opening Balance Parser")
        print("=" * 80)
        print("Total Ledgers          :", total_ledgers)
        print("Bill Allocations       :", total_bill_allocations)
        print("Lookup Size            :", len(lookup))

        if lookup:

            first_key = next(iter(lookup))

            print("\nSample Key :")

            print(first_key)

            print("\nSample Value :")

            print(lookup[first_key])

        print("=" * 80)

        return lookup

    ###########################################################################
    # MERGE BILLS RECEIVABLE + OPENING BALANCE
    ###########################################################################

    def _merge_bills(self, bills, opening_lookup):
        """
        Merge Bills Receivable data with Opening Balance lookup.

        Matching Key:
            (normalized Party Name, normalized Bill Reference)

        This previously matched on raw clean()'d strings, which meant any
        case difference, extra whitespace, or HTML-escaped character
        between the two Tally exports caused a bill to silently fail to
        match even though it was really the same bill. Using the
        normalize_party/normalize_bill_ref helpers (which existed in the
        original file but were never called) fixes that.

        Returns
        -------
        List[dict]
        """

        matched = 0
        unmatched = 0

        merged = []

        for bill in bills:

            party = self.clean(
                bill.get("party", "")
            )

            bill_ref = self.clean(
                bill.get("bill_ref", "")
            )

            key = (
                self.normalize_party(party),
                self.normalize_bill_ref(bill_ref),
            )

            opening = opening_lookup.get(key)

            ###################################################################
            # Match Found
            ###################################################################

            if opening:

                matched += 1

                bill["opening_balance"] = opening.get(
                    "opening_balance",
                    ""
                )

                #
                # Prefer Opening XML Bill Date if Bills XML doesn't contain it.
                #
                if (
                    not bill.get("bill_date")
                    and opening.get("bill_date")
                ):
                    bill["bill_date"] = opening["bill_date"]

                bill["credit_period"] = opening.get(
                    "credit_period",
                    ""
                )

            ###################################################################
            # No Match
            ###################################################################

            else:

                unmatched += 1

                bill["opening_balance"] = ""

                bill["credit_period"] = ""

            merged.append(bill)

        #######################################################################
        # Debug Summary
        #######################################################################

        print("=" * 80)
        print("Merge Summary")
        print("=" * 80)

        print("Bills Receivable :", len(bills))
        print("Opening Records  :", len(opening_lookup))
        print("Matched Bills    :", matched)
        print("Unmatched Bills  :", unmatched)

        if unmatched:

            print("\nSample Unmatched Bills:\n")

            count = 0

            for bill in merged:

                if bill["opening_balance"] == "":

                    print(
                        (
                            bill["party"],
                            bill["bill_ref"],
                        )
                    )

                    count += 1

                    if count == 10:
                        break

        print("=" * 80)

        return merged

    ###########################################################################
    # MAIN METHOD
    ###########################################################################

    def get_all(self):
        """
        Fetch complete Pending Bills information.

        Flow
        ----
        1. Fetch Bills Receivable XML
        2. Parse Bills Receivable
        3. Fetch Opening Balance XML
        4. Parse Opening Balance
        5. Merge both datasets
        6. Return final response
        """

        print("\n" + "=" * 80)
        print("STARTING PENDING BILLS FETCH")
        print("=" * 80)

        #######################################################################
        # Company Name
        #######################################################################

        company_name = self.get_company_name()

        print("Company :", company_name)

        #######################################################################
        # Parse Bills Receivable
        #######################################################################

        print("\nParsing Bills Receivable...")

        bills = self._parse_bills_receivable()

        print(f"Bills Parsed : {len(bills)}")

        #######################################################################
        # Parse Opening Balance
        #######################################################################

        print("\nParsing Opening Balances...")

        opening_lookup = self._parse_opening_balances()

        print(f"Opening Records : {len(opening_lookup)}")

        #######################################################################
        # Merge
        #######################################################################

        print("\nMerging Bills...")

        merged = self._merge_bills(
            bills,
            opening_lookup,
        )

        #######################################################################
        # Statistics
        #######################################################################

        opening_found = sum(
            1
            for row in merged
            if row.get("opening_balance")
        )

        opening_missing = len(merged) - opening_found

        #######################################################################
        # Debug Summary
        #######################################################################

        print("\n" + "=" * 80)
        print("FINAL SUMMARY")
        print("=" * 80)

        print("Company Name        :", company_name)
        print("Bills Count         :", len(merged))
        print("Opening Found       :", opening_found)
        print("Opening Missing     :", opening_missing)

        if merged:

            print("\nFirst Record\n")

            for key, value in merged[0].items():
                print(f"{key:20}: {value}")

        print("=" * 80)

        #######################################################################
        # Final Response
        #######################################################################

        return {

            "success": True,

            "company_name": company_name,

            "count": len(merged),

            "matched_opening_balance": opening_found,

            "missing_opening_balance": opening_missing,

            "data": merged,

        }