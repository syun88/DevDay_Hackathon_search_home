"""Read public SUUMO detail tables for an already retrieved listing only."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from suumo_bridge import _curl_get


class DetailParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.capture=None; self.parts=[]; self.label=None; self.fields={}; self.title=''
    def handle_starttag(self,tag,attrs):
        if tag in ('th','td','h1'):
            self.capture=tag; self.parts=[]
        elif tag=='br' and self.capture: self.parts.append(' / ')
    def handle_data(self,data):
        if self.capture: self.parts.append(data)
    def handle_endtag(self,tag):
        if tag != self.capture: return
        value=' '.join(''.join(self.parts).split())[:1500]
        if tag=='h1': self.title=value[:180]
        elif tag=='th': self.label=value[:100]
        elif self.label and value and len(self.fields)<40:
            self.fields[self.label]=value; self.label=None
        self.capture=None


def read_details(url):
    # Never allow arbitrary URLs, query strings, redirects to user-selected hosts,
    # cookies, or page-issued instructions to extend the fetch surface.
    if not isinstance(url,str) or not re.fullmatch(r'https://suumo\.jp/chintai/bc_\d{6,20}/',url):
        raise ValueError('Unsupported source detail URL')
    parser=DetailParser()
    parser.feed(_curl_get(url,max_bytes=2_500_000,timeout=12).decode('utf-8',errors='replace'))
    if not parser.fields: raise ValueError('Source details unavailable or listing removed')
    return {'source_fields':parser.fields,'source_title':parser.title,
            'details_fetched_at':datetime.now(timezone.utc).isoformat(),
            'details_note':'Source-provided details, not independently verified vacancy or fee completeness.'}
