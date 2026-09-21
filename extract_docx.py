import zipfile
import xml.etree.ElementTree as ET
import sys
import re

def extract_text_from_docx(docx_path):
    """Extract text from a .docx file by parsing word/document.xml"""
    with zipfile.ZipFile(docx_path, 'r') as z:
        with z.open('word/document.xml') as f:
            xml_content = f.read()
    
    # Parse XML
    root = ET.fromstring(xml_content)
    
    # Define namespaces
    ns = {
        'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    }
    
    lines = []
    
    # Iterate through paragraphs
    for para in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
        para_text = []
        for node in para.iter():
            tag = node.tag.split('}')[-1] if '}' in node.tag else node.tag
            if tag == 't':
                if node.text:
                    para_text.append(node.text)
            elif tag == 'tab':
                para_text.append('\t')
            elif tag == 'br' or tag == 'cr':
                para_text.append('\n')
        # Also check for text in w:t directly
        full_text = ''.join(para_text)
        if full_text.strip():
            lines.append(full_text)
        else:
            lines.append('')  # preserve empty paragraphs
    
    return '\n'.join(lines)

if __name__ == '__main__':
    docx_path = r'c:\Users\zhaoting1\.trae-cn\attachments\6a5a0344613c941065ff6e3e\90e0e470-3d1d-4712-b872-fdec95ffb9ad_a91dfa0c-a977-40a4-81ef-1ef02f1b9ab8_通用规则.docx'
    text = extract_text_from_docx(docx_path)
    print(text)
    
    # Also save to a file
    output_path = r'c:\Users\zhaoting1\Desktop\GEO生文审核 - 副本\docx_content.txt'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'\n\n--- Content saved to: {output_path} ---', file=sys.stderr)
