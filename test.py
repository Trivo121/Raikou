import urllib.request
req = urllib.request.Request('http://localhost:8000/api/v1/ingestion/upload', method='POST', headers={'Content-Type': 'multipart/form-data; boundary=boundary'}, data=b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="test.txt"\r\n\r\ntest\r\n--boundary--\r\n')
try:
    print(urllib.request.urlopen(req).read())
except Exception as e:
    print(e)
