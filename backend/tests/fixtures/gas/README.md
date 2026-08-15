# GAS fixture provenance

These fixtures preserve two different official GAS identifier namespaces. They are evidence
for fail-closed parsing; their visually corresponding UIK labels do not establish identifier
equivalence.

## `altai-result-tik-node.json`

This is the exact `tvdTreeJson` object for the selected TIK, reserialized as UTF-8 JSON without
changing its values, from the official GAS result response preserved by the Internet Archive at
2021-10-07 07:51:41 UTC:

<https://web.archive.org/web/20211007075141id_/http://www.vybory.izbirkom.ru/region/izbirkom?action=show&root=1000058&tvd=22220002523303&vrn=100100225883172&prver=0&pronetvd=null&region=22&sub_region=22>

The original response is Windows-1251 HTML. Its source URL is shared by result records for
UIKs 592–614. It gives UIK 592 the election-result node ID `9229002199809`.

## `altai-commission-tik-children.json`

This is the complete official GAS JSON response to the commission-directory request:

`http://www.altai-terr.vybory.izbirkom.ru/region/altai-terr?action=ikTree&region=22&vrn=22220001926860&onlyChildren=true`

The response was preserved byte-for-byte as
`orig/altai-terr/tik/22220001926860_childs.js` in GIS-Lab's 14 September 2021 original-source
archive (`cik_20210914_orig.7z`, SHA-256
`10343b6ce5c6ada2276832520fa79460df896aecaa67a7ed48816707763de542`). The archive is
published at <https://gis-lab.info/data/cik/cik_20210914_orig.7z>; its provenance and schema
are documented at <https://gis-lab.info/qa/cik-data.html>.

This response gives UIK 592 the commission-directory ID `9229002166846`. The corresponding
official commission page is:

<http://www.altai-terr.vybory.izbirkom.ru/region/altai-terr?action=ik&vrn=9229002166846>
