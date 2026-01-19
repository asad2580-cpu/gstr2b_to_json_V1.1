import json
import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET
from xml.dom import minidom

# --- UTILS ---
def r2(val):
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def prettify(elem):
    rough_string = ET.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="    ")

def get_input_file_terminal():
    """Terminal-based file selection for Codespaces/Linux environments"""
    print("\nAvailable JSON files in current directory:")
    files = [f for f in os.listdir('.') if f.endswith('.json')]
    
    if not files:
        print("No .json files found in the current directory.")
    else:
        for i, f in enumerate(files):
            print(f" [{i}] {f}")

    path = input("\nEnter the filename (e.g., data.json) or the number from above: ").strip()
    
    # Handle if user typed a number
    if path.isdigit() and int(path) < len(files):
        return files[int(path)]
    
    # Handle if user typed the name
    if os.path.exists(path):
        return path
    else:
        print(f"Error: File '{path}' not found.")
        return None

# --- CORE FUNCTIONS ---

def process_gstr2b_raw(input_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    data_root = raw_data.get("data", {})
    return_period = data_root.get("rtnprd", "N/A")
    b2b_sections = data_root.get("docdata", {}).get("b2b", [])
    
    extracted_invoices = []
    for supplier in b2b_sections:
        supplier_name = supplier.get("trdnm")
        supplier_gstin = supplier.get("ctin")
        for inv in supplier.get("inv", []):
            extracted_invoices.append({
                "supplier_name": supplier_name,
                "supplier_gstin": supplier_gstin,
                "date": inv.get("dt"),
                "invoice_number": inv.get("inum"),
                "return_period": return_period,
                "taxable_value": inv.get("txval", 0),
                "igst_amount": inv.get("igst", 0),
                "cgst_amount": inv.get("cgst", 0),
                "sgst_amount": inv.get("sgst", 0),
                "total_invoice_value": inv.get("val", 0)
            })
    
    output_path = "cleaned_invoices.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(extracted_invoices, f, indent=4)
    return extracted_invoices, output_path

def generate_masters(invoices, company_name):
    envelope = ET.Element("ENVELOPE", {"VERSION": "1.0"})
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"
    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    req_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(req_desc, "REPORTNAME").text = "All Masters"
    static_vars = ET.SubElement(req_desc, "STATICVARIABLES")
    ET.SubElement(static_vars, "SVCURRENTCOMPANY").text = company_name
    req_data = ET.SubElement(import_data, "REQUESTDATA")

    created = set()
    def create_ledger_elem(name, group, is_gst=False, gst_head=None):
        if name in created: return
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        ledger = ET.SubElement(msg, "LEDGER", {"NAME": name, "ACTION": "Create"})
        ET.SubElement(ledger, "NAME").text = name
        ET.SubElement(ledger, "PARENT").text = group
        ET.SubElement(ledger, "ISBILLWISEON").text = "Yes" if group == "Sundry Creditors" else "No"
        if is_gst:
            ET.SubElement(ledger, "TAXTYPE").text = "GST"
            ET.SubElement(ledger, "GSTDUTYHEAD").text = gst_head
        created.add(name)

    create_ledger_elem("Round Off", "Indirect Expenses")
    for inv in invoices:
        taxable = float(inv["taxable_value"])
        igst, cgst, sgst = float(inv.get("igst_amount", 0)), float(inv.get("cgst_amount", 0)), float(inv.get("sgst_amount", 0))
        create_ledger_elem(inv["supplier_name"], "Sundry Creditors")
        if igst > 0 and taxable > 0:
            rate = round((igst / taxable) * 100)
            create_ledger_elem(f"Interstate Purchase {rate}%", "Purchase Accounts")
            create_ledger_elem(f"Input IGST {rate}%", "Duties & Taxes", True, "Integrated Tax")
        if (cgst > 0 or sgst > 0) and taxable > 0:
            rate = round(((cgst + sgst) / taxable) * 100)
            create_ledger_elem(f"Local Purchase {rate}%", "Purchase Accounts")
            create_ledger_elem(f"Input CGST {rate//2}%", "Duties & Taxes", True, "Central Tax")
            create_ledger_elem(f"Input SGST {rate//2}%", "Duties & Taxes", True, "State Tax")

    output_file = "masters_import.xml"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(prettify(envelope))
    return output_file

def generate_vouchers(invoices, company_name):
    xml_str = f"""<ENVELOPE>
    <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
    <BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>
    <STATICVARIABLES><SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY></STATICVARIABLES>
    </REQUESTDESC><REQUESTDATA>"""

    for inv in invoices:
        vch_date = datetime.strptime(inv['date'], "%d-%m-%Y").strftime("%Y%m%d")
        taxable = r2(inv['taxable_value'])
        igst, cgst, sgst = r2(inv.get('igst_amount', 0)), r2(inv.get('cgst_amount', 0)), r2(inv.get('sgst_amount', 0))
        invoice_total = r2(inv['total_invoice_value'])
        tax_total = igst + cgst + sgst
        debit_calc = taxable + tax_total
        rate = int((tax_total / taxable * 100).quantize(0)) if taxable != 0 else 0
        purchase_ledger = f"Interstate Purchase {rate}%" if igst > 0 else f"Local Purchase {rate}%"

        xml_str += f"""
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
            <VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJTYPE="Voucher">
                <DATE>{vch_date}</DATE>
                <REFERENCEDATE>{vch_date}</REFERENCEDATE>
                <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
                <REFERENCE>{inv['invoice_number']}</REFERENCE>
                <VOUCHERNUMBER>{inv['invoice_number']}</VOUCHERNUMBER>
                <PARTYLEDGERNAME>{inv['supplier_name']}</PARTYLEDGERNAME>
                <PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>{inv['supplier_name']}</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                    <AMOUNT>{invoice_total}</AMOUNT>
                    <BILLALLOCATIONS.LIST>
                        <NAME>{inv['invoice_number']}</NAME>
                        <BILLTYPE>New Ref</BILLTYPE>
                        <AMOUNT>{invoice_total}</AMOUNT>
                    </BILLALLOCATIONS.LIST>
                </ALLLEDGERENTRIES.LIST>
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>{purchase_ledger}</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                    <AMOUNT>-{taxable}</AMOUNT>
                </ALLLEDGERENTRIES.LIST>"""

        if igst > 0:
            xml_str += f"""
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>Input IGST {rate}%</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                    <AMOUNT>-{igst}</AMOUNT>
                </ALLLEDGERENTRIES.LIST>"""
        else:
            xml_str += f"""
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>Input CGST {rate//2}%</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                    <AMOUNT>-{cgst}</AMOUNT>
                </ALLLEDGERENTRIES.LIST>
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>Input SGST {rate//2}%</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                    <AMOUNT>-{sgst}</AMOUNT>
                </ALLLEDGERENTRIES.LIST>"""

        if debit_calc != invoice_total:
            diff = invoice_total - debit_calc
            deemed = "No" if diff > 0 else "Yes"
            xml_str += f"""
                <ALLLEDGERENTRIES.LIST>
                    <LEDGERNAME>Round Off</LEDGERNAME>
                    <ISDEEMEDPOSITIVE>{deemed}</ISDEEMEDPOSITIVE>
                    <AMOUNT>{-diff}</AMOUNT>
                </ALLLEDGERENTRIES.LIST>"""

        xml_str += "</VOUCHER></TALLYMESSAGE>"

    xml_str += "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    output_file = "vouchers_import.xml"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(xml_str)
    return output_file

# --- MAIN ---
def main():
    print("========================================")
    print("   GSTR-2B TO TALLY AUTONOMOUS TOOL")
    print("========================================\n")

    input_path = get_input_file_terminal()
    if not input_path:
        return

    company_name = input("\nEnter Tally Company Name: ").strip()
    if not company_name: company_name = "Test Company"

    try:
        invoices, cleaned_json = process_gstr2b_raw(input_path)
        m_file = generate_masters(invoices, company_name)
        v_file = generate_vouchers(invoices, company_name)
        
        print("\n" + "="*30)
        print(f"SUCCESS!")
        print(f"1. Cleaned JSON: {cleaned_json}")
        print(f"2. Masters XML:  {m_file}")
        print(f"3. Vouchers XML: {v_file}")
        print("="*30)
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    main()