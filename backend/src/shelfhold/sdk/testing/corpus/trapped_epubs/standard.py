# SPDX-License-Identifier: MIT
# ruff: noqa: E501
"""A standard EPUB 3, which the neutralizer must let through (non-regression of the whitelist)."""

from __future__ import annotations

from shelfhold.sdk.testing.corpus.trapped_epubs.build import EPUB_NS, XHTML_NS, Member, zip_bytes

X = XHTML_NS
E = EPUB_NS
_FILL = bytes(range(256)) * 160


def _opf(ch_href: str = "Text/ch1.xhtml", extra: str = "") -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:isbn:9780000000000</dc:identifier>
<dc:title>Livre</dc:title><dc:language>fr</dc:language><dc:creator id="c">A. Auteur</dc:creator>
<meta refines="#c" property="role" scheme="marc:relators">aut</meta><meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
<meta name="cover" content="cover-img"/></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="cover-img" href="Images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>
<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml" properties="svg"/>
<item id="ch1" href="{ch_href}" media-type="application/xhtml+xml"/>
<item id="css" href="Styles/style.css" media-type="text/css"/>
<item id="f1" href="Fonts/Body.otf" media-type="font/otf"/>
<item id="f2" href="Fonts/Body.woff2" media-type="font/woff2"/>
<item id="f3" href="Fonts/Old.ttf" media-type="application/vnd.ms-opentype"/>
{extra}</manifest>
<spine toc="ncx"><itemref idref="cover" linear="no"/><itemref idref="ch1"/></spine>
<guide><reference type="cover" title="Couverture" href="Text/cover.xhtml"/><reference type="text" href="{ch_href}#debut"/></guide>
</package>'''.encode()


NAV = f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="{X}" xmlns:epub="{E}" lang="fr"><head><title>Sommaire</title></head>
<body><nav epub:type="toc" id="toc"><h1>Sommaire</h1><ol><li><a href="Text/ch1.xhtml#debut">Chapitre 1</a></li></ol></nav>
<nav epub:type="landmarks" hidden="hidden"><ol><li><a epub:type="cover" href="Text/cover.xhtml">Couverture</a></li></ol></nav></body></html>'''.encode()
NCX = b"""<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd"><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="x"/></head><docTitle><text>Livre</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Ch 1</text></navLabel><content src="Text/ch1.xhtml"/></navPoint></navMap></ncx>"""
COVER = f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="{X}" xmlns:epub="{E}"><head><title>Couverture</title></head><body>
<div><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 600 800" preserveAspectRatio="xMidYMid meet"><image width="600" height="800" xlink:href="../Images/cover.jpg"/></svg></div></body></html>'''.encode()
CH = f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="{X}" xmlns:epub="{E}" lang="fr" xml:lang="fr"><head><title>Ch 1</title><link rel="stylesheet" type="text/css" href="../Styles/style.css"/></head>
<body epub:type="bodymatter"><section epub:type="chapter" id="debut"><h1>Chapitre 1</h1><p>Texte <em>accentué</em> &#8212; <a href="#n1" epub:type="noteref">1</a></p>
<aside epub:type="footnote" id="n1"><p>Note.</p></aside><p><img src="../Images/cover.jpg" alt="c"/></p></section></body></html>'''.encode()
CSS = b"""@font-face { font-family: "Body"; src: url("../Fonts/Body.otf") format("opentype"), url(../Fonts/Body.woff2) format("woff2"); font-weight: normal; }
@font-face { font-family: Old; src: url('../Fonts/Old.ttf'); }
body { font-family: "Body", serif; margin: 0 5%; } h1 { text-align: center; }"""
JPEG = (
    bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300")
    + _FILL[:20000]
    + b"\xff\xd9"
)
OTF = b"OTTO\x00\x0c" + _FILL[:30000]
WOFF2 = b"wOF2\x00\x01" + _FILL[:30000]
TTF = b"\x00\x01\x00\x00" + _FILL[:30000]


def standard_epub3(
    ch_href: str = "Text/ch1.xhtml", extra_opf: str = "", extra: tuple[Member, ...] = ()
) -> bytes:
    """A standard EPUB 3 (nav, NCX, SVG cover page, local cover, fonts, css urls, guide)."""
    return zip_bytes(
        [
            Member("mimetype", b"application/epub+zip", deflate=False),
            Member(
                "META-INF/container.xml",
                b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
            ),
            Member("OEBPS/content.opf", _opf(ch_href, extra_opf)),
            Member("OEBPS/nav.xhtml", NAV),
            Member("OEBPS/toc.ncx", NCX),
            Member("OEBPS/Text/cover.xhtml", COVER),
            Member("OEBPS/" + ch_href.replace("%20", " ").replace("%3A", ":"), CH),
            Member("OEBPS/Styles/style.css", CSS),
            Member("OEBPS/Images/cover.jpg", JPEG, deflate=False),
            Member("OEBPS/Fonts/Body.otf", OTF),
            Member("OEBPS/Fonts/Body.woff2", WOFF2),
            Member("OEBPS/Fonts/Old.ttf", TTF),
            *extra,
        ]
    )
