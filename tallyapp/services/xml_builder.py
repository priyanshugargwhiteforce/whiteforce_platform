from xml.sax.saxutils import escape
FINANCIAL_YEARS = {
    "2023-24": ("20230401", "20240331"),
    "2024-25": ("20240401", "20250331"),
    "2025-26": ("20250401", "20260331"),
    "2026-27": ("20260401", "20270331"),
}

MONTHS = {
    "01": "January",  "02": "February", "03": "March",
    "04": "April",    "05": "May",      "06": "June",
    "07": "July",     "08": "August",   "09": "September",
    "10": "October",  "11": "November", "12": "December",
}


def tally_date(yyyymmdd):
    """
    20261001 -> 1-Oct-2026. Tally only applies SVFROMDATE/SVTODATE to a
    Voucher collection when they're sent as TYPE="Date" in this format;
    with plain YYYYMMDD it scans every voucher in the company.
    """
    return f"{int(yyyymmdd[6:8])}-{MONTHS[yyyymmdd[4:6]][:3]}-{yyyymmdd[:4]}"


def get_ledger_xml():
    return """
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>List of Ledgers</ID>
        </HEADER>
        <BODY>
            <DESC>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="List of Ledgers">
                            <TYPE>Ledger</TYPE>
                            <FETCH>Name</FETCH>
                        </COLLECTION>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """


def get_voucher_xml(ledger_name, fy="2026-27", from_date=None, to_date=None):
    safe_name = escape(ledger_name)
    if from_date and to_date:
        fd, td = from_date, to_date
    else:
        fd, td = FINANCIAL_YEARS.get(fy, ("20260401", "20270331"))

    return f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>VoucherCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVFROMDATE TYPE="Date">{tally_date(fd)}</SVFROMDATE>
                    <SVTODATE TYPE="Date">{tally_date(td)}</SVTODATE>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="VoucherCollection">
                            <TYPE>Voucher</TYPE>
                            <FETCH>
                                MASTERID,
                                GUID,
                                DATE,
                                VOUCHERNUMBER,
                                VOUCHERTYPENAME,
                                PARTYLEDGERNAME,
                                AMOUNT,
                                NARRATION,
                                ALLLEDGERENTRIES.LIST.LEDGERNAME,
                                ALLLEDGERENTRIES.LIST.AMOUNT,
                                ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE
                            </FETCH>
                            <FILTERS>LedgerFilter, DateRangeFilter</FILTERS>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="LedgerFilter">
                            $PartyLedgerName="{safe_name}"
                        </SYSTEM>
                        <SYSTEM TYPE="Formulae" NAME="DateRangeFilter">
                            $Date &gt;= ##SVFromDate AND $Date &lt;= ##SVToDate
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """


def get_all_ledgers_with_vouchers_xml(fy="2026-27", from_date=None, to_date=None):
    if from_date and to_date:
        fd, td = from_date, to_date
    else:
        fd, td = FINANCIAL_YEARS.get(fy, ("20260401", "20270331"))

    return f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>VoucherCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVFROMDATE TYPE="Date">{tally_date(fd)}</SVFROMDATE>
                    <SVTODATE TYPE="Date">{tally_date(td)}</SVTODATE>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="VoucherCollection">
                            <TYPE>Voucher</TYPE>
                            <FETCH>
                                MASTERID,
                                GUID,
                                DATE,
                                VOUCHERNUMBER,
                                VOUCHERTYPENAME,
                                PARTYLEDGERNAME,
                                AMOUNT,
                                NARRATION,
                                ALLLEDGERENTRIES.LIST.LEDGERNAME,
                                ALLLEDGERENTRIES.LIST.AMOUNT,
                                ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE
                            </FETCH>
                            <FILTERS>DateRangeFilter</FILTERS>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="DateRangeFilter">
                            $Date &gt;= ##SVFromDate AND $Date &lt;= ##SVToDate
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """


def get_voucher_detail_xml(master_id):
    # Accepts either the MASTERID (as returned by the vouchers list) or the GUID.
    safe_id = escape(str(master_id))
    return f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>VoucherDetailCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="VoucherDetailCollection">
                            <TYPE>Voucher</TYPE>
                            <FETCH>
                                MASTERID,
                                GUID,
                                DATE,
                                VOUCHERNUMBER,
                                VOUCHERTYPENAME,
                                PARTYLEDGERNAME,
                                AMOUNT,
                                NARRATION,
                                ALLLEDGERENTRIES.LIST.LEDGERNAME,
                                ALLLEDGERENTRIES.LIST.AMOUNT,
                                ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE
                            </FETCH>
                            <FILTERS>VoucherFilter</FILTERS>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="VoucherFilter">
                            $GUID = "{safe_id}" OR $$String:$MasterID = "{safe_id}"
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """

def get_group_ledgers_xml(group_name):
    """
    Fetch all LEDGERS whose PARENT is `group_name`, along with
    Opening Balance, Nett Transactions, and Closing Balance —
    mirrors Tally's Group Summary (Opening/Nett/Closing) view.
    """
    safe_name = escape(group_name)
    return f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>GroupLedgerCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="GroupLedgerCollection">
                            <TYPE>Ledger</TYPE>
                            <FETCH>
                                NAME,
                                PARENT,
                                OPENINGBALANCE,
                                CLOSINGBALANCE
                            </FETCH>
                            <FILTERS>ParentGroupFilter</FILTERS>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="ParentGroupFilter">
                            $Parent="{safe_name}"
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """
def get_ledger_opening_balance_xml(ledger_name):
    safe_name = escape(ledger_name)
    return f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>LedgerOpeningBalance</ID>
        </HEADER>
        <BODY>
            <DESC>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="LedgerOpeningBalance">
                            <TYPE>Ledger</TYPE>
                            <FETCH>
                                NAME,
                                OPENINGBALANCE
                            </FETCH>
                            <FILTERS>LedgerNameFilter</FILTERS>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="LedgerNameFilter">
                            $Name="{safe_name}"
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """