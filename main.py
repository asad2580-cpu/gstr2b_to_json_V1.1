import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import List

# FastAPI Imports
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="GSTR-2B to Tally API")

# Enable CORS so your React app can talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your Vercel URL
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- UTILS (Kept from your script) ---
def r2(val):
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def prettify(elem):
    rough_string = ET.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="    ")

# --- MODIFIED CORE FUNCTIONS (Now returning strings/data instead of writing files) ---

def process_gstr2b_logic(raw_data: dict):
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
    return extracted_invoices

def generate_masters_string(invoices, company_name):
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

    return prettify(envelope)

def generate_vouchers_string(invoices, company_name):
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
    return xml_str

# --- THE API ENDPOINT ---

@app.post("/process-gst")
async def process_gst(file: UploadFile = File(...), company_name: str = Form(...)):
    try:
        # Read the uploaded file
        content = await file.read()
        raw_json = json.loads(content)
        
        # 1. Clean data
        invoices = process_gstr2b_logic(raw_json)
        
        # 2. Generate XML strings
        masters_xml = generate_masters_string(invoices, company_name)
        vouchers_xml = generate_vouchers_string(invoices, company_name)
        
        # 3. Return everything as a JSON response (Option A)
        return {
            "success": True,
            "company": company_name,
            "cleaned_data": invoices,
            "masters_xml": masters_xml,
            "vouchers_xml": vouchers_xml
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)