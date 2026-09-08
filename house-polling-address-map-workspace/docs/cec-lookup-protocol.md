# Russian CEC address → UIK lookup protocol

Checked 2026-09-08. This note records only behavior visible in first-party CEC
HTML/JavaScript/API responses preserved by the Internet Archive, plus the result
of attempting to reach the live origin from this workspace. It is not an API
contract.

## Bottom line

The CEC has exposed two materially different implementations:

1. **2015–2018 address tree:** an enumerable, cascading JSON tree at
   `/services/lk_tree/`; a terminal address identifier is resolved by an HTML
   result page at `/services/lk_address/{address-id}?do=result`.
2. **2019–at least 2023 autocomplete API:** free-text autocomplete at
   `/iservices/voter-services/address/search/{term}/`; selecting a terminal
   result causes a JSON lookup at
   `/iservices/voter-services/committee/address/{address-id}`.

The second protocol is the most recent one that could be verified from CEC's
own archived source. The live CEC host was not reachable from this environment,
so its status in 2026 is **unverified**. Do not call the 2019–2023 protocol
"current" until a browser on a network that can reach the site captures one
successful lookup.

## Live-origin check

On 2026-09-08 both `cikrf.ru` and `www.cikrf.ru` resolved here to
`5.143.246.137`. TCP connection attempts to HTTP and HTTPS timed out before an
HTTP response (curl exit 28; 20–30 second limits). The public page and a direct
autocomplete URL behaved the same way. A fetch through Jina's external reader
also timed out at the origin.

Consequences:

- No 2026 response headers, cookies, CAPTCHA, WAF challenge, request quota, or
  rate-limit response was observed.
- A connection timeout does not distinguish downtime, routing/geographic
  filtering, or silent firewall drops. It is not evidence of a particular
  anti-bot product.
- The implementation should keep the live protocol behind an adapter and begin
  with a fresh browser-network capture from a reachable network.

The advertised entry page is
<https://www.cikrf.ru/digital-services/naydi-svoy-izbiratelnyy-uchastok/>.
The latest archived first-party page recovered in this pass is [the 6 July 2023
capture](https://web.archive.org/web/20230706032023id_/http://www.cikrf.ru/digital-services/naydi-svoy-izbiratelnyy-uchastok/).

## Protocol A: 2019–2023 autocomplete and JSON resolver

### Evidence and request sequence

The archived page loads a CEC-owned common script and a page-specific script:

- [`/digital-services/assets/js/script.js` (16 September 2021
  capture)](https://web.archive.org/web/20210916170911id_/http://cikrf.ru/digital-services/assets/js/script.js?163171177657593)
- [`/digital-services/naydi-svoy-izbiratelnyy-uchastok/script.js` (6 July 2023
  page capture)](https://web.archive.org/web/20230706032023id_/http://www.cikrf.ru/digital-services/naydi-svoy-izbiratelnyy-uchastok/script.js?16239446648750)

The relevant browser sequence is:

```text
GET /iservices/voter-services/address/search/{encodeURIComponent(term)}/
  → JSON array of address suggestions

select a suggestion where leaf == true

GET /iservices/voter-services/committee/address/{suggestion.id}
  → JSON commission/UIK object
```

Both calls are same-origin GETs. The source supplies no request body, bearer
token, API key, CSRF value, or custom authentication header. jQuery may add its
normal `X-Requested-With` header to same-origin AJAX requests, but the
application source does not explicitly require it.

### Autocomplete request

Exact route:

```http
GET /iservices/voter-services/address/search/{term}/
Accept: application/json
```

Client behavior visible in the CEC JavaScript:

- Select2 waits 250 ms after input (`delay: 250`). This is a client debounce,
  not evidence of a server quota.
- The UI starts at one input character (`minimumInputLength: 1`).
- Before URL encoding, it strips every character outside
  `[a-zA-ZА-Яа-я0-9\s]`. Notably, this expression excludes `Ё/ё` and
  punctuation such as hyphens.
- An empty value is converted to one space.
- Results are cached only in a page-local JavaScript object, keyed by the
  sanitized term.
- A non-terminal suggestion (`leaf: false`) cannot be selected. Its display
  name plus `, ` is put back into the search field and queried again.
- A terminal suggestion (`leaf: true`) supplies the opaque `id` passed to the
  resolver.
- There is no pagination, offset, limit, or continuation parameter in the
  first-party client.

Archived first-party response headers for [the query `1` on 10 September
2021](https://web.archive.org/web/20210910143540id_/http://cikrf.ru/iservices/voter-services/address/search/1/)
record `Content-Type: application/json;charset=UTF-8`. That captured response
contained exactly ten results, but one sample is not enough to prove that ten
is a fixed server limit.

Sanitized/truncated example of the response shape (values are public address
records; only three of the ten captured records are shown):

```json
[
  {
    "id": "138452579881574400000352602",
    "name": "Республика Дагестан, Агульский район, село Тпиг, 10-0круг",
    "leaf": true
  },
  {
    "id": "135637829943431680000414749",
    "name": "Московская область, Одинцовский городской округ, Район 1",
    "leaf": false
  },
  {
    "id": "158155828251325440000467930",
    "name": "Приморский край, город Владивосток, Советский район, проспект 100-летия Владивостока, д. 100/В, кв. 1",
    "leaf": true
  }
]
```

The sample proves that apartment-qualified records can be terminal. A
nationwide crawl cannot assume that resolving one building always covers every
apartment.

### Address-ID resolver

Exact route used by `loadCommission(params)` when `params.addressId` exists:

```http
GET /iservices/voter-services/committee/address/{address-id}
Accept: application/json
```

The page script passes the result directly to the same renderer used for
lookups by region code and UIK number. Consequently its expected response is a
commission object of this shape:

```json
{
  "vrn": "4014001117979",
  "name": "Участковая избирательная комиссия №2",
  "subjCode": "01",
  "numKsa": "01T001",
  "vid": "5",
  "address": {
    "address": "385200, Республика Адыгея, ... проспект имени В.И.Ленина, 16",
    "descr": "здание МБОУ СОШ№1",
    "phone": "8-87772-9-23-72",
    "lat": "44.882893",
    "lon": "39.187187"
  },
  "votingAddress": {
    "address": "385200, Республика Адыгея, ... проспект имени В.И.Ленина, 16",
    "descr": "здание МБОУ СОШ№1",
    "phone": "8-87772-9-23-72",
    "lat": "44.882893",
    "lon": "39.187187"
  }
}
```

This example is from the first-party archived [UIK-number route for subject 01,
UIK 2](https://web.archive.org/web/20201031034444id_/http://cikrf.ru/iservices/voter-services/committee/subjcode/01/num/2),
which returns the same object consumed by the renderer. No archived capture of
a successful `/committee/address/{id}` response was found in this pass, so the
route and expected shape are verified from CEC source, while the illustrative
values come from the sibling resolver. Preserve this distinction in tests.

`address` is the commission's own premises. `votingAddress` is the polling
room. They must remain separate fields even when their values happen to match.

### Related routes, not required for address → UIK

The same archived CEC source also uses:

```text
GET /iservices/voter-services/committee/subjcode/{subject-code}/num/{uik-number}
GET /iservices/voter-services/committee/{vrn}/tree/
GET /iservices/voter-services/committee/{vrn}/members/
GET /iservices/voter-services/vibory/committee/{vrn}
```

The first is a useful independent UIK resolver. The others supply parent
commissions, members, and elections and are unnecessary for the core mapping.

### Cookies, tokens, and failure handling

- The 2023 page HTML contains a normal Bitrix `bitrix_sessid` value, but the
  lookup JavaScript does not read or send it in any lookup URL/body.
- A 2021 archived autocomplete response records this origin header (secret
  redacted):

  ```http
  Set-Cookie: session-cookie=<redacted>; Max-Age=86400; Path=/; HttpOnly
  ```

  The initial autocomplete request succeeds without the client first acquiring
  or explicitly submitting that cookie in application code. The evidence does
  not establish whether subsequent calls require or merely accept it. A real
  client should retain cookies within a session until tested otherwise.
- The application's misleadingly named `cookie()` helper actually stores the
  last address, region/UIK, and selected tab in `localStorage`; those values are
  convenience state, not authentication.
- The JavaScript treats a response object containing `ajaxNoJson` as an error
  sentinel and otherwise relies on ordinary AJAX failure callbacks. The
  archived source shows no CAPTCHA integration or challenge-response field.
- No archived first-party evidence of HTTP 429 behavior, quotas, or a published
  rate limit was found. The 250 ms UI delay must not be reported as a server
  allowance.

## Protocol B: 2015–2018 enumerable address tree

### Entry and hierarchy requests

The archived [address-choice page](https://web.archive.org/web/20160330045443id_/http://cikrf.ru/services/lk_address/?do=address)
creates a jsTree whose data source is:

```http
GET /services/lk_tree/?first=1&id=%23
```

For an ordinary child node it issues:

```http
GET /services/lk_tree/?id={node.id}
```

For root nodes carrying the special `ret` attribute (the archived root assigns
`ret=0` to Moscow and `ret=1` to Saint Petersburg), the URL callback uses:

```http
GET /services/lk_tree/?ret={0|1}&id={node.id}
```

The `id` query parameter is added by jsTree's data callback. Archives also
contain `/services/lk_tree/?first=1` without the explicit `id=%23`; clients
should follow the exact current response rather than assume the root requires
both fields.

Archived response headers identify
`Content-Type: application/json; charset=windows-1251`. Example root shape:

```json
[
  {
    "id": "6434066820",
    "text": "Россия",
    "a_attr": {"intid": "", "levelid": "1"},
    "state": {"opened": true, "selected": true},
    "children": [
      {
        "id": "6434185746",
        "text": "Республика Адыгея (Адыгея)",
        "a_attr": {
          "intid": "135637827259064320000370671",
          "levelid": "2"
        },
        "children": true
      }
    ]
  }
]
```

Source: [18 September 2016 first-level JSON
capture](https://web.archive.org/web/20160918014903id_/http://cikrf.ru/services/lk_tree/?first=1%26id=%23).

Normal branch responses are arrays of the same child-node objects. A recovered
path demonstrates that the tree can descend through subject, city/district,
locality, street, house, and apartment-like levels:

```text
Республика Саха (Якутия)
  → город Якутск
  → Якутск
  → улица Петра Алексеева
  → дом 21/1
  → 57
```

The corresponding node `levelid` values were `2`, `4`, `6`, `7`, `8`, and
`11`. Treat `levelid` as source metadata rather than hard-coding these observed
semantics nationwide.

Expanding a terminal node returns the empty JSON array `[]`. The CEC page then
redirects using that node's `a_attr.intid`:

```http
GET /services/lk_address/{a_attr.intid}?do=result
```

`node.id` and `a_attr.intid` are different identifiers. Root node IDs changed
between archived runs, so `node.id` should be treated as traversal/session
state. The final resolver uses `intid`.

### Result response

The resolver returns `text/html; charset=windows-1251`, not JSON. A recovered
[16 September 2018 result](https://web.archive.org/web/20180916081756id_/http://cikrf.ru/services/lk_address/135670815465285120000390023?do=result)
contains:

```text
Участковая избирательная комиссия №2248
Номер Территориальной избирательной комиссии: 030
Адрес помещения УИК: 191015, Город Санкт-Петербург, ... Дегтярный переулок, дом 24, школа №174
Телефон УИК: 8(812)5739793
Адрес помещения для голосования: 191015, ... Дегтярный переулок, дом 24, школа №174
Телефон помещения для голосования: 8(812)5739793
```

Again, parse and retain the commission-premises and voting-room fields
separately.

The archived tree source contains no token, CAPTCHA, auth field, or cookie
handling. The recovered responses do not establish a historical rate limit.

## Implementation implications

### What is safely implementable now

- Implement two versioned adapters, not one collection of guessed fallbacks.
- For the old tree, decode JSON and result HTML as Windows-1251, preserve both
  node identifiers, recurse until `[]`, and resolve each terminal `intid`.
- For the newer API, reproduce the browser's exact sanitization and UTF-8 URL
  encoding, retain a cookie jar, accept both `leaf` states, and resolve only
  terminal IDs.
- Persist the raw response, URL, retrieval/archive timestamp, encoding, and a
  hash before parsing.
- Model apartment-qualified address IDs. Do not collapse them into a building
  until all apartment mappings for that building agree.
- Cache by full request URL and make traversal resumable. Historic node IDs are
  not durable external identifiers.

### What is not established

- That either protocol remains live in 2026.
- That autocomplete can be enumerated completely. Its client has no pagination,
  and a broad captured query returned ten mixed nationwide matches.
- That an address ID is permanent across CEC data refreshes.
- That one request per 250 ms is an acceptable server rate.
- That the `session-cookie` is optional for the resolver.
- That all Russian addresses terminate at a house rather than an apartment or
  another sub-house qualifier.

### Required pre-crawl capture

From a browser that can reach the live service, record one lookup for a known
address and one lookup by region/UIK. Preserve a HAR and raw response bodies,
then verify:

1. the loaded script URLs and their hashes;
2. exact autocomplete and resolver URLs/methods;
3. request/response headers and redirect chain;
4. whether a fresh session can call the resolver without first loading the
   page or autocomplete;
5. cookies and their rotation/expiry;
6. zero-result, non-terminal, invalid-ID, and too-fast-request responses;
7. result cap/pagination behavior using a deliberately broad term;
8. whether repeated requests trigger 403, 429, CAPTCHA, or silent delay.

Until that capture is complete, the 2019–2023 adapter should be disabled by
default for live nationwide execution. The 2015–2018 tree is the stronger
historical path for exhaustive enumeration because it exposes child lists;
the autocomplete protocol is suitable for resolving externally supplied
GAR/FIAS addresses but does not itself demonstrate completeness.
