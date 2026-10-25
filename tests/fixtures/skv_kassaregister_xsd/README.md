Skatteverket's XML schema for "Export av data från journalminne enligt
SKVFS 2021:16" (version 2021-12-08), from the schemalager zip
https://www.skatteverket.se/download/18.5dc1d8b31903014b1bf11b9/1719999713439/xml-schema-kassaregister.zip
(downloaded 2026-10-24). Same folder layout and file names as the zip,
without the xs:annotation/documentation blocks and comments (that is most
of the original size). Every element, type, occurrence and restriction is
unchanged: a normalized signature of each file was compared with the
original and matched.
tests/test_kassa_xml.py validates Booqy's export against it.
