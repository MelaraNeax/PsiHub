import httpx

url = 'https://mi-servidor-api-42204102300.southamerica-east1.run.app/api/translate-file'
files = {'file': ('test.pdf', b'%PDF-1.4 test', 'application/pdf')}
try:
    r = httpx.post(url, files=files, timeout=10)
    print(r.status_code)
    print(r.text)
except Exception as e:
    print(e)
