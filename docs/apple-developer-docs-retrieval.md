# Retrieving Apple Developer Documentation Source Data

This document describes how to retrieve the machine-readable source data behind
Apple's MDM-related developer documentation web pages. The goal is to enable later
comparison between Apple's published API definitions and the hand-crafted JSON
Schemas maintained in the
github.com/micromdm/apple-device-services repository (e.g. `dep/schemas/*.json`,
`anb/schemas/*.json`).

## Background

Apple's developer documentation pages are HTML, but they are entirely rendered
client-side by JavaScript. A plain `curl`/`WebFetch` of a page such as:

```
https://developer.apple.com/documentation/devicemanagement/devicelistrequest
```

returns only a skeleton: `<meta>` tags plus a handful of `<script>` tags. The
real content is injected into the DOM by a JavaScript single-page app (SPA).

Underneath that SPA, Apple serves a **machine-readable JSON source** of each
documentation page. That JSON is the "source document" we want, because it
contains the structured schema-like data (property names, types, descriptions,
cross-references) without requiring a browser.

These pages are very likely [Swift-DocC-Render](https://github.com/swiftlang/swift-docc-render) —
a Vue.js SPA — rendering DocC's **render JSON**, the machine-readable output
Swift-DocC emits inside a `.doccarchive`. The inference is based on the Vue.js
SPA and the JSON's field names (`primaryContentSections`, `references`,
`variants`, `schemaVersion`, `symbolKind`, etc.) described below.

The REST endpoint sections (`restEndpoint`, `restBody`, `restResponses`) and the
`externalID` scheme (e.g. `mdm-services:dep:DeviceListRequest`,
`rest:dep:post:server-devices`) suggest these pages are machine-generated from
internal OpenAPI-style specifications, converted into DocC's render JSON.

## Retrieving the source JSON (the deterministic URL)

For the documentation namespaces this repo covers — `devicemanagement` (DEP +
ANB) and `applebusinessapi` (ABM) — the JSON is located at a deterministic URL.

Given a documentation page URL (`x-apple-developer-api-uri` in our schemas):

```
https://developer.apple.com/documentation/<path>
```

the corresponding JSON source document is:

```
https://developer.apple.com/tutorials/data/documentation/<path>.json
```

In other words, apply these two transforms:

1. Replace `developer.apple.com/documentation/` with
   `developer.apple.com/tutorials/data/documentation/`
2. Append `.json`

Examples:

| HTML page (`x-apple-developer-api-uri`) | JSON source document |
|---|---|
| `.../documentation/devicemanagement/devicelistrequest` | `.../tutorials/data/documentation/devicemanagement/devicelistrequest.json` |
| `.../documentation/devicemanagement/device` | `.../tutorials/data/documentation/devicemanagement/device.json` |
| `.../documentation/devicemanagement/devicelistresponse/devices-data.dictionary` | `.../tutorials/data/documentation/devicemanagement/devicelistresponse/devices-data.dictionary.json` |
| `.../documentation/applebusinessapi/orgdevice` | `.../tutorials/data/documentation/applebusinessapi/orgdevice.json` |

Nested dictionary paths (including dashes in `*-data.dictionary`) work
unchanged. The JSON is plain `application/json` and can be fetched with a simple
HTTP GET; no JavaScript execution is required.

### Self-validation

Each JSON document contains a `variants` array whose first entry lists the
canonical HTML path(s):

```json
"variants": [
  { "paths": [ "/documentation/devicemanagement/devicelistrequest" ] }
]
```

This can be used to confirm that the JSON document corresponds to the expected
HTML page.

### Scope and limitations

- Works for the `devicemanagement` (DEP + ANB) and `applebusinessapi` (ABM)
  namespaces. All `x-apple-developer-api-uri` values across `dep/`, `anb/`, and
  `abm/` resolve to HTTP 200 under this transform.
- The ABM namespace was previously `applebusinessmanagerapi`; Apple renamed it
  to `applebusinessapi`. Schemas using the old name will 404 until their
  `x-apple-developer-api-uri` is updated (see the repo's PR #11).
- Does not apply to `support.apple.com/guide/...` URLs (e.g. the GDMF schema).
- The `required` signal is **inconsistent across namespaces**: Apple's
  per-property `required` flag is populated and meaningful in some namespaces
  (e.g. ANB — `href`, `id`, and `type` on `Apps` are `true`), but is `false`
  for *every* property in others (e.g. DEP). Because it cannot be trusted
  wholesale, the sync tool captures it but treats it as additive: it emits a
  `required` array when generating fresh schemas, and when merging it *adds*
  Apple-declared required properties without ever dropping the repo's
  hand-derived `required` entries.

## Structure of the source JSON

The JSON document has a stable shape. Relevant fields:

- `abstract[]` — the page description (maps to our `description`).
- `metadata` — symbol identity:
  - `title` — the type name (maps to our `title`).
  - `symbolKind` — e.g. `dictionary` (data type) or `httpRequest` (endpoint).
  - `roleHeading` — e.g. `Object`.
  - `fragments[]` — declaration tokens (e.g. `object DeviceListRequest`).
  - `externalID` — a stable internal identifier.
- `primaryContentSections[]` — the schema body. The section with
  `kind: "properties"` holds `items[]`, one per property:
  - `name` — property name.
  - `type[]` — array of tokens describing the type. Tokens can be a scalar name
    (`string`, `boolean`), a bracketed array (`[string]`, or a referenced type
    tokenized as `[`, `typeIdentifier`, `]`), or a bare `typeIdentifier`:
    - `{"kind":"text","text":"string"}` — a scalar type name.
    - `{"kind":"text","text":"[string]"}` — an array of strings.
    - `{"kind":"typeIdentifier","identifier":"doc://…","text":"Foo.Bar"}` — a
      reference to a nested `*-data.dictionary` type (resolve via `references`,
      see below).
  - `required` — boolean, but **unreliable**: meaningful in some namespaces
    (e.g. ANB), yet `false` for every property in others (e.g. DEP); see
    "Scope and limitations".
  - `attributes[]` — constraints (often empty). An entry with
    `kind: "allowedValues"` carries a structured enum as its `values` array
    (e.g. `{"kind":"allowedValues","values":["apps"]}` for `Apps.type`); this is
    authoritative and maps to JSON Schema `enum`.
  - `content[]` — prose description as inline tokens (see below).
- `references` — related symbols keyed by `doc://` identifier.
- `schemaVersion` — version of the data format.

### Important: detail is split between inline tokens and structured fields

The per-property `type` is a bare token (`string`, `boolean`, `[string]`). Some
richer detail is embedded in the description `content[].inlineContent[]` token
stream:

| token `type` | meaning | example |
|---|---|---|
| `text` | prose, including version gating | "This key is valid in X-Server-Protocol-Version 10 and later." |
| `codeVoice` | literal / enum value | `iPad`, `added`, `modified` |
| `reference` | cross-link (doc:// identifier) | `Get Replacement Details` |

Some details are now structured fields rather than inline-token heuristics:

- `required` is a per-property boolean (see "Structure of the source JSON"),
  but unreliable across namespaces (see "Scope and limitations").
- `enum` is available as `attributes[].allowedValues` for properties Apple
  generates from an allowed-values list (e.g. `Apps.type` → `["apps"]`).

What still lives only in the inline tokens is `format: date-time` (the
"ISO 8601" prose) and version-gating information, so those remain heuristics.
This is why the repo's schemas are "hand-crafted" rather than purely
mechanically generated: the source JSON is a strong signal for field names,
types, descriptions, and (sometimes) `required`/`enum`, but not a drop-in
schema.

### Mapping Apple tokens to JSON Schema

For a property item, the `type` token maps to JSON Schema as follows:

| Apple `type` token | JSON Schema |
|---|---|
| `{"kind":"text","text":"string"}` | `{"type":"string"}` |
| `{"kind":"text","text":"boolean"}` | `{"type":"boolean"}` |
| `{"kind":"text","text":"[string]"}` | `{"type":"array","items":{"type":"string"}}` |
| `{"kind":"typeIdentifier","identifier":"doc://…","text":"Foo.Bar"}` | `$ref` to the sibling `Foo.Bar.json` schema file |

Two other per-property fields map directly, with the `required` caveat from
"Scope and limitations":

| Apple field | JSON Schema |
|---|---|
| `required: true` | top-level `"required": [...]` listing the property name |
| `attributes[].allowedValues` | property-level `"enum": [...]` |

A `typeIdentifier` token also carries `preciseIdentifier` (e.g.
`mdm-services:dep:DeviceListResponse.Devices`), which matches the nested
dictionary's `metadata.externalID`. The nested type's own JSON document is
fetched with the same transform — append the `*-data.dictionary` path and
`.json`.

### Maps ("Any Key" dictionaries)

Apple encodes a dictionary with arbitrary string keys (a map) as a nested
`*-data.dictionary` whose single property is literally named `Any Key`; the
value type of `Any Key` is the map's value type (which may itself be a `$ref`).
These are still generated as their own schema files, but the `Any Key` property
is emitted as an `additionalProperties` map rather than a named property:

| Apple nested dictionary | generated schema |
|---|---|
| `Foo.Bar` = `{ "Any Key": "string" }` | `{"type":"object","additionalProperties":{"type":"string"}}` |
| `Foo.Bar` = `{ "Any Key": Device }` | `{"type":"object","additionalProperties":{"$ref":"Device.json"}}` |

This keeps a 1:1 mapping with Apple's `*-data.dictionary` files. The repo's
hand-crafted schemas follow the same convention (e.g.
`ClearProfileResponse.devices` is an `additionalProperties` map).

The `reference` tokens inside `content[].inlineContent[]` likewise point to
`doc://…` identifiers. All `doc://…` identifiers resolve against the
`references` map in the same document, which lists `title` and `url` for each
related symbol.

### Endpoint pages (request/response types)

A `symbol` page is one of two flavors, distinguished by `metadata.symbolKind`:

- `dictionary` — a data type; it has a `properties` section (see above).
- `httpRequest` — a web service endpoint; instead of `properties` it has
  `restEndpoint`, `restBody`, and `restResponses` sections:

  - `restEndpoint` — the HTTP method, base URL, and path as `tokens[]`
    (`kind` of `method`, `baseURL`, `path`).
  - `restBody` — the request body; `bodyContentType[]` holds a
    `typeIdentifier` token naming the request data dictionary.
  - `restResponses` — one entry per response code (`items[].status`,
    `items[].reason`); each item's `type[]` holds a `typeIdentifier` naming
    the response data dictionary.

The `typeIdentifier` carries an `identifier` (`doc://…` URI, resolvable via the
page's `references` map to the dictionary's `url`) and a `preciseIdentifier`
(e.g. `mdm-services:dep:FetchDeviceRequest`) that matches the dictionary's
`metadata.externalID`. The request/response dictionaries are themselves fetched
with the same `/documentation/` → `/tutorials/data/documentation/` + `.json`
transform.

## The index / navigation pages

Apple's landing pages (the "collections" behind the left-column navigation) are
themselves JSON documents. For example the Device Management landing page

```
https://developer.apple.com/documentation/devicemanagement
```

maps to `.../tutorials/data/documentation/devicemanagement.json`. Its JSON
encodes the entire left-nav index in two keys:

- `topicSections[]` — the nav groups. Each entry has `title`, `anchor`, and
  `identifiers[]` (a list of `doc://…` URIs).
- `references` — a map from each `doc://…` URI to `{title, url, type, role}`.

Resolving an identifier gives the API doc link (`references[id].url`) and the
kind of page (`references[id].role`). Applying the deterministic transform to
`url` yields that page's tutorial JSON.

### Roles and recursion

The `role` (and `type`) values classify each item and drive a crawl:

| `role` | `type` | meaning | has schema? |
|---|---|---|---|
| `collection` | `topic` | module landing page | no — recurse |
| `collectionGroup` | `topic` | sub-group landing page | no — recurse |
| `symbol` | `topic` | leaf API endpoint or data dictionary | `dictionary` yes, `httpRequest` no (see "Endpoint pages") |
| `article` | `topic` | prose article | no |
| `overview` | `topic` | "Technologies" root | no |

### Extracting the full inventory

1. Start from a landing URL, fetch its `.json` (the deterministic transform).
2. For each `topicSections[].identifiers[]`, resolve `references[id]` →
   `{title, url, role}`.
3. Recurse into `collection`/`collectionGroup` pages (each such page's own
   `.json` has its own `topicSections`/`references`).
4. Record `symbol` pages as leaves: their `url` is the API doc link, and the
   transformed URL is the tutorial JSON.
5. For each endpoint `symbol` (`metadata.symbolKind == "httpRequest"`), follow
   the `restBody.bodyContentType[].identifier` and
   `restResponses[].items[].type[].identifier` `typeIdentifier` tokens to the
   request/response data dictionaries (resolved via `references`), and record
   those too.
6. For each dictionary's properties, follow nested `typeIdentifier` tokens
   (e.g. `SeedBuildToken`, `Apps.Attributes`) and recurse. Dotted names are
   nested dictionaries generated as their own files (e.g. `Apps.Attributes.json`),
   not inlined. Map placeholders (see "Maps (`Any Key` dictionaries)") are also
   generated as files, rendered as `additionalProperties` maps.

The navigation tree alone is **not** the complete inventory: the request/response
data dictionaries (e.g. `DeviceListRequest`, `FetchDeviceResponse`) are not
listed in `topicSections` — they are reachable only through the endpoint pages'
`restBody`/`restResponses` `typeIdentifier` links. The nav's "Objects and data
types" group lists the standalone dictionaries (e.g. `Device`, `MachineInfo`,
`Profile`, `Limit`, `Url`), but not the request/response types.

Caveat: `references` is per-document, not global — an identifier only resolves
against the `references` of the document that listed it in its `topicSections`.
Cross-page references (e.g. `typeIdentifier`/`reference` tokens on symbol pages)
resolve against *that page's* `references` instead.

### Scoping by repository directory

A documentation namespace contains many unrelated sections, so a crawler should
**not** walk the whole tree. Seed it with one root per schema directory and
recurse only within that root, using `role` to stop at section boundaries
(`collection`/`collectionGroup` recurse; `symbol` collect; `article` skip):

| Repo directory | Root | role |
|---|---|---|
| `dep/` | `https://developer.apple.com/documentation/devicemanagement/device-assignment` | collectionGroup |
| `anb/` | `https://developer.apple.com/documentation/devicemanagement/app-book-and-subscription-management` | collectionGroup |
| `abm/` | `https://developer.apple.com/documentation/applebusinessapi` | collection |
| `asm/` (future) | `https://developer.apple.com/documentation/appleschoolmanagerapi` | collection |
| *(not in repo)* | `https://developer.apple.com/documentation/devicemanagement/roster-management` | collectionGroup |

Notes:

- The repo's `anb/` corresponds to the **new** App and Book management API
  (`app-book-and-subscription-management`), not the legacy
  `app-and-book-management-legacy` group (whose data types are `VppAsset`,
  `VppAssignment`, `VppLicense`, etc.).
- `applebusinessapi` (ABM) and `appleschoolmanagerapi` (ASM) are siblings under
  the higher-level `apple-school-and-business-manager-api` collection. A future
  `asm/` directory would map to `appleschoolmanagerapi`; its schemas are
  expected to be very similar to `abm/`'s.
- Within `dep/`, the "Objects and data types" section holds the schema types
  (e.g. `Device`, `Profile`, `Limit`, `Url`, `MachineInfo`), while the endpoint
  `symbol` pages carry the request/response types.

## Discovery and breadcrumbs

This records how the JSON source was located and where the URL construction
lives in Apple's shipped JavaScript, so both can be re-derived if Apple
redeploys or refactors their app.

### How the source was found

1. **Observed that plain fetching yields no content.** A normal fetch of
   `https://developer.apple.com/documentation/devicemanagement/devicelistrequest`
   returned only `<meta>` tags and `<script>` tags, no body content. The page is
   rendered entirely by JavaScript.

2. **Rendered the page with a browser.** Using Selenium against a remote Chrome
   (Chrome for Testing), the rendered DOM was captured. It contained a
   `Properties` section (`id="properties"`) with a `parameters-table
   property-table` and per-property rows (`.property-name` + `.property-type`),
   but **no embedded JSON** — searching the rendered HTML for
   `application/json`, `schema`, `openapi`, `swagger`, or `*.json` links found
   nothing.

3. **Noted the SPA's own asset paths.** The page's `<script>` tags referenced
   `/tutorials/js/chunk-vendors*.js`, `/tutorials/js/chunk-common*.js`,
   `/tutorials/js/index*.js`, and `/tutorials/js/analytics.js`. The `/tutorials/`
   path revealed the "tutorials" documentation platform behind the SPA.

4. **Confirmed the fetch logic in the SPA's JavaScript.** The page's
   `chunk-vendors` bundle contains the code that builds and fetches the data URL.
   It is **not** a guess — the URL derivation is present verbatim in the shipped
   JavaScript (see below).

5. **Confirmed the pattern is deterministic.** The derivation returned HTTP 200
   across a sample of `devicemanagement` URLs (top-level symbols and nested
   `*-data.dictionary` paths). The `variants[].paths[]` field in each JSON
   confirmed the mapping back to the original HTML path. An initial check of the
   ABM namespace appeared to 404, but that was the old `applebusinessmanagerapi`
   name — Apple renamed it to `applebusinessapi`, which resolves fine under the
   same transform.

### Which asset

The relevant code is in the **`chunk-vendors`** bundle, loaded from the
documentation page's `<script>` tags:

```
<script defer src="/tutorials/js/chunk-vendors.3a1bc248.js">
<script defer src="/tutorials/js/chunk-common.233ff197.js">
<script defer src="/tutorials/js/index.9dc03288.js">
<script src="/tutorials/js/analytics.js">
```

The asset filenames are **content-hashed** (`.3a1bc248`, etc.), so they will
change when Apple redeploys. To re-discover them: fetch any documentation page's
HTML and read the `<script src="/tutorials/js/...">` tags, then download the
`chunk-vendors.*.js` file. No source map is published (`.js.map` returns 404).

The documentation-page component itself is **lazy-loaded** in separate chunks
(not in the entry bundles above). The router entry in `index.*.js` names the
chunk IDs for the `/documentation*` route (e.g. `702`, `408`, `840`, `27`), and
`index.*.js` also contains the webpack chunk map that resolves each ID to its
content-hashed filename:

```js
{26:"5ec0611a",27:"c5890a72",...,408:"989eb60f",...,702:"ca594055",...,840:"2349e834",...}
```

so a chunk is fetched from `/tutorials/js/<id>.<hash>.js` (e.g.
`/tutorials/js/27.c5890a72.js`). Re-discover them by grepping `index.*.js` for
`topicSections`-related chunk IDs, or simply for the chunk map `{<id>:"<hash>"}`.

### What to look for

The `chunk-vendors` bundle contains the app's base URL and the data-URL
builder/parser, in minified form:

```js
// base URL of the documentation SPA
BASE_URL:"/tutorials/"

// build the data URL from a route path
function f(e){const t=e.replace(/\/$/,"");return`${(0,r.Fd)(["/data",t])}.json`}

// inverse: parse a data URL back into a route path
function p(e){const{pathname:t,search:n}=new URL(e),r=/\/data(\/.*).json$/,i=r.exec(t);return i?i[1]+n:t+n}
```

and the fetcher that consumes it:

```js
async function l(e,t={},n={}){function r(e){return!e.ok}
  const a=(0,i.Lo)(e),s=(0,i.vk)(t);
  s&&(a.search=s);
  const u=await fetch(a.href,n);
  if(r(u))throw u;
  if(u.redirected)throw new c({location:u.url,response:u});
  const l=await u.json();
  return(0,o.Ay)(l.schemaVersion),l
}
```

and the router entry that handles documentation pages:

```js
{ path:"/documentation*", name:r.Zf, component:()=>Promise.all([...]).then(...) }
```

### How the pieces fit together

- The Vue Router route `path: "/documentation*"` captures documentation URLs.
- The route path (e.g. `/documentation/devicemanagement/devicelistrequest`) is
  passed to `f()`, which strips a trailing slash and returns
  `join(["/data", path]) + ".json"` → `/data/documentation/devicemanagement/devicelistrequest.json`.
- That relative path is resolved against `BASE_URL: "/tutorials/"`, producing
  `https://developer.apple.com/tutorials/data/documentation/devicemanagement/devicelistrequest.json`.
- `l()` then `fetch()`es that URL, parses the JSON, and validates
  `schemaVersion` (`(0,o.Ay)(l.schemaVersion)`).
- The inverse function `p()` confirms the round-trip: it matches
  `/\/data(\/.*).json$/` against the pathname and extracts the inner path,
  i.e. `/tutorials/data/documentation/<path>.json` → `/documentation/<path>`.

The regex `/\/data(\/.*).json$/` in `p()` is the most robust breadcrumb: it
encodes the entire `<path>.json` naming convention in one line.

### The nav/index rendering

The left-column navigation and page content are rendered by the lazy-loaded
documentation chunk (chunk `27`), which destructures `topicSections`,
`references`, and `seeAlsoSections` directly from the JSON document — confirming
the JSON is the source of both content and navigation.

- Nav links resolve via the `references` map, then router navigation:
  ```js
  references[e].url   // identifier -> target path
  this.$router.push(t)
  ```
- The role→type switch also lives here:
  ```js
  type:({role:e})=>{switch(e){
    case Se.y.collection:     return _e.t.module;
    case Se.y.collectionGroup:return _e.t.collection;
    default:                  return e}}
  ```

The role/type taxonomy strings are defined in chunk `702`:

```js
const r={article:"article",codeListing:"codeListing",collection:"collection",
  collectionGroup:"collectionGroup",containerSymbol:"containerSymbol",
  devLink:"devLink",dictionarySymbol:"dictionarySymbol",generic:"generic", ...}
```

The same chunk `27` also renders endpoint pages: it contains the
`restEndpoint`, `restBody`, `restResponses`, `restParameters`,
`bodyContentType`, and `httpRequest` symbols, matching the `primaryContentSections`
kinds described under "Endpoint pages (request/response types)".
