# Russian CEC address → UIK lookup protocol

Checked 2026-09-08. This note records behavior visible in first-party CEC
HTML/JavaScript/API responses captured from the live services or preserved by
the Internet Archive. It is not an API contract.

## Bottom line

The CEC has exposed three materially different implementations:

1. **Live 2026 gateway:** an unauthenticated GraphQL address index at
   `apps.cikrf.ru/service/address/search/node/graphql`, followed by the
   arithmetic-challenge-authenticated `ik-inp-service-pbcopy` gateway. This
   route was probed successfully on 8 September 2026 and returned a 2026 State
   Duma UIK for current building- and apartment-level address identifiers.
2. **2015–2018 address tree:** an enumerable, cascading JSON tree at
   `/services/lk_tree/`; a terminal address identifier is resolved by an HTML
   result page at `/services/lk_address/{address-id}?do=result`.
3. **2019–at least 2023 autocomplete API:** free-text autocomplete at
   `/iservices/voter-services/address/search/{term}/`; selecting a terminal
   result causes a JSON lookup at
   `/iservices/voter-services/committee/address/{address-id}`.

The first protocol is current. The older two remain useful as historical
evidence and, in the tree's case, as a possible enumerable snapshot. The
2019–2023 routes are retired on the live host: even after acquiring a page
session and sending browser/AJAX headers, both old paths returned the site's
generic HTML page rather than JSON.

## Live-origin check

Direct TCP/HTTP requests from this environment still time out. The configured
repository SOCKS proxy reaches the live hosts over plain HTTP:

- the CEC page returned HTTP 200 and UTF-8 HTML;
- its `apps.cikrf.ru/service/gateway/` iframe returned HTTP 200;
- the current GraphQL and authenticated JSON endpoints returned HTTP 200.

HTTPS connections through this particular proxy failed, while the Moscow
regional commission's HTTPS service was directly reachable. This is a routing
property of the observed environment, not evidence that the official service
does not support HTTPS elsewhere. Proxy credentials were neither logged nor
stored with captured responses.

The advertised entry page is
<https://www.cikrf.ru/digital-services/naydi-svoy-izbiratelnyy-uchastok/>.
The live page body captured on 8 September 2026 has SHA-256
`333bfc3f3b8da65eda8bddaf7f360d555323650050563d46c18349fb4f54f9cb`.

## Protocol C: live 2026 gateway

### Browser bootstrap

The live page embeds:

```html
<iframe src="http://apps.cikrf.ru/service/gateway/?origin=http://www.cikrf.ru">
```

The iframe loads `/service/gateway/env.js` and a hashed JavaScript application.
The captured environment file identifies these two backends:

```text
http://apps.cikrf.ru/service/address/search/node
http://apps.cikrf.ru/service/ik-inp-service-pbcopy
```

The captured main application JavaScript has SHA-256
`bc57879cc4154849fb28ef7f644b050ae976dacb49e7807e5f72848589f2b00a`.

### Address autocomplete and parent expansion

Autocomplete is a same-origin GraphQL POST and does not require the gateway API
key:

```http
POST /service/address/search/node/graphql
Content-Type: application/json
```

```graphql
query ($query: String!) {
  ADDRESSSearchFulltext(
    query: $query
    limit: 20
    incomplete: true
    unchecked: false
    locations: ["1"]
  ) {
    id
    fullName
    isRetro
  }
}
```

After the user selects an address, the client obtains every ancestor ID:

```graphql
query ($query: ID!) {
  nodeADDRESSById(id: $query) {
    id
    parents { id VCADDRESS_NAME }
  }
}
```

The selected ID and all parent IDs are passed as one comma-separated
`addressIds` value to the election gateway. Apartment-qualified nodes are live
and significant; they must not be collapsed to their building before comparing
assignments.

### Arithmetic challenge and election lookup

The election gateway requires two headers:

```http
X-Api-Key: <ephemeral value>
X-Client-Fingerprint: <browser user-agent>
```

The key is minted by this public browser flow:

```text
GET  /service/ik-inp-service-pbcopy/challenge/get
POST /service/ik-inp-service-pbcopy/challenge/solve
```

`challenge/get` returned `jsTask` in the form `return A + B;` and a public
token. The solve body contains that token, the arithmetic answer, and the exact
user-agent fingerprint. The solve response contains the API key. No cookies
were created during the observed flow. Challenge bodies must not be archived
because the solve response contains a live credential.

With the key, the live application calls:

```text
GET /addresses/elections?page=1&perPage=100&addressIds={ids}
GET /addresses/commissionClassifiers?addressIds={ids}&electionsId={numeric-id}
GET /addresses/commissionOrgs?page=1&perPage=100&addressIds={ids}
```

The 2026 State Duma identifiers observed on 8 September 2026 are:

```json
{
  "id": 587813923,
  "externalId": "2b72bb97-c625-4a02-a76b-b5740c4d5f6a",
  "votingDate": "2026-09-20"
}
```

`commissionClassifiers` requires the numeric `id`, not the UUID
`externalId`. Passing the UUID produced HTTP 400.

### Successful canaries

Three selected current addresses were resolved against election `587813923`:

| Selected address level | Subject | Result |
| --- | --- | --- |
| Moscow apartment | 77 | UIK 146 |
| Vladivostok apartment | 25 | UIK 840 |
| Izobilny building | 26 | UIK 456 |

For the Vladivostok canary, the search, parent, election, and classifier body
hashes were respectively
`1e928c2bc11d0c24eb349ef654adb0d42fc59a1631349514551d4e4ffc35ed5c`,
`1b91582ee2a5356fc6d60f27c1f20adc7292b55f6166cb0875034b4fa4efe41c`,
`bb421e855eac16ad8a0122bf17e5666da457ca28195889d4cb0ff242e5912d91`,
and `33d125f4200459719b0c848cab5bbc2f80c8774557011d3e1d5623569829f02e`.

The federal `addresses/commissionOrgs` response was empty for all three
canaries. Thus the federal flow currently proves **home address -> 2026 UIK
number**, but does not by itself provide the physical voting-room address for
those examples.

Moscow's independent official service did provide that final field. For
`Тверская улица, 6 стр. 1`, its address resolver returned UIK 146, voting date
20 September 2026, and the voting place at `ПЕТРОВКА УЛ., 23/10 стр. 21`,
`ГБОУ школа № 2054`. The response body SHA-256 is
`28a472e8107c7f4ce8566d8733b697b86c0897f5ea563cef8c1bc7b44febbaa9`.

### Negative and rate canaries

- Calling the election gateway without the challenge headers returned HTTP 401.
- A nonsense GraphQL address returned an empty result array with HTTP 200.
- A nonsense `addressIds` value returned an empty JSON object with HTTP 200.
- Five immediate authenticated classifier requests and five immediate GraphQL
  searches all returned HTTP 200. This small canary is not evidence of a bulk
  crawling allowance or stable rate limit.

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

- Use the 2026 GraphQL + authenticated election-gateway protocol for current
  address-to-UIK resolution. Keep its temporary authentication response out of
  the raw evidence store.
- Keep all three protocol generations in separate versioned adapters.
- For the old tree, decode JSON and result HTML as Windows-1251, preserve both
  node identifiers, recurse until `[]`, and resolve each terminal `intid`.
- For the 2019–2023 API, reproduce the browser's exact sanitization and UTF-8 URL
  encoding, retain a cookie jar, accept both `leaf` states, and resolve only
  terminal IDs.
- Persist the raw response, URL, retrieval/archive timestamp, encoding, and a
  hash before parsing.
- Model apartment-qualified address IDs. Do not collapse them into a building
  until all apartment mappings for that building agree.
- Cache by full request URL and make traversal resumable. Historic node IDs are
  not durable external identifiers.

### What is not established

- That the federal 2026 gateway can supply a physical voting-room address for
  every returned UIK; its commission-organization projection was empty in the
  three-address canary.
- That autocomplete can be enumerated completely. Its client has no pagination,
  and the 2026 client explicitly limits each query to 20 results.
- That an address ID is permanent across CEC data refreshes.
- That one request per 250 ms is an acceptable server rate.
- That the `session-cookie` is optional for the resolver.
- That all Russian addresses terminate at a house rather than an apartment or
  another sub-house qualifier.

### Required pre-crawl validation

Before a nationwide run, expand the current canary and verify:

1. whether address and parent IDs remain stable across gateway deployments;
2. how many regions populate `addresses/commissionOrgs` and when;
3. regional sources for physical voting-room addresses where it is empty;
4. whether a fresh session can reuse an API key and its observed expiry;
5. whether any address assignments differ at apartment level;
6. malformed and partial address behavior across a representative sample;
7. whether the 20-result cap can hide exact GAR buildings;
8. whether sustained requests trigger 403, 429, CAPTCHA, or silent delay.

The 2019–2023 adapter should remain disabled for live execution. The 2015–2018
tree is the stronger historical path for exhaustive enumeration because it
exposes child lists. The 2026 autocomplete is suitable for resolving externally
supplied GAR/FIAS addresses but does not itself demonstrate completeness.
