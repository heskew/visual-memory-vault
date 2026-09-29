# Share to Vault (iOS Shortcut)

Share from Photos or Safari queues one Visual Memory Vault job and stops. The shortcut does not wait for Gemini, extract, or a receipt.

Public iCloud link: *(not published yet)*. A signed link can only be created in the Shortcuts app on a phone, with **Copy iCloud Link**, by the Apple ID that publishes it. That does not need the Apple Developer Program. After it exists, replace this paragraph with that link. People who open it review every action, answer the two import questions, and then add the shortcut.

Until that link exists, build the shortcut from the steps below. The steps use only ordinary Shortcuts actions. Do not add Run JavaScript on Webpage, Run Script, or any Scripting action.

## What it sends

The live proxy is the only host this shortcut accepts:

`https://visual-memory-vault-proxy-151358874679.us-east1.run.app`

- `https://visual-memory-vault-proxy-151358874679.us-east1.run.app/upload`
- `https://visual-memory-vault-proxy-151358874679.us-east1.run.app/capture/stitch`
- `https://visual-memory-vault-proxy-151358874679.us-east1.run.app/capture/url`

| Share sheet input | Request | Body |
| --- | --- | --- |
| 1 image | `POST /upload` | multipart, one field named `file` |
| 2 to 8 images | `POST /capture/stitch` | multipart, one `file` field per image, in share order |
| No image, and an `http` or `https` URL (Safari's page link) | `POST /capture/url` | JSON `{"url":"…"}` |

`subject` is optional. Leave it out of the shortcut unless you want a label. To add one later, add a form field named `subject` on upload and stitch, or a `subject` key on the URL dictionary. The proxy keeps at most 500 characters.

If a share contains both images and a URL, the images win: one image goes to `/upload`, two or more go to `/capture/stitch`. Safari's share button sends the page URL and no image, so a page goes to `/capture/url`.

A successful call returns `202` JSON:

```json
{
  "status": "accepted",
  "job_id": "<uuid>",
  "image_path": "/media/<uuid>_…"
}
```

The shortcut then shows:

```text
Queued
job_id: <uuid>
image_path: /media/…
```

Anything else shows `Not queued` and the proxy `detail` string when the body has one. That covers `4xx` from a bad key, a bad stitch, or an oversized image. The shortcut does not call `POST /ingest` and does not poll `GET /jobs/{job_id}`.

Auth is the header production upload already uses: `X-Api-Key` set to the proxy `PROXY_API_KEY`. The key lives only in the shortcut on the phone. Do not type the API key into this file, the README, or the shortcut you publish.

## One-time setup on the phone

1. Open **Shortcuts** → **+** → name it **Share to Vault**.
2. Tap the shortcut name, then the receive line at the top. Set it to **Receive Images, URLs, and Safari web pages input from Share Sheet**.
3. Turn on **Show in Share Sheet**. Share types: Images, URLs, Safari web pages.
4. Add the actions in the next section, in order.
5. In the first **Text** action, put the live proxy URL above (no path, no key).
6. In the second **Text** action, put the proxy API key.
7. Those two text actions are the saved values. The next share does not ask again.

Confirm one photo, two photos, and a Safari page. Each result should be `Queued` plus a `job_id`, not a written summary of the picture. Then publish, using the section after the actions, so the key is not inside the shared shortcut.

## Actions

Search names are the labels in the Shortcuts app. Tap an action's result and choose **Rename** where a name is given. List indexes start at 1.

### 1. Base URL and API key

**Text.** Contents: the live proxy URL. Rename the result `Vault Base URL`.

**Text.** Contents: the API key, on this phone only. Rename the result `Vault API Key`.

Before you publish, both fields become **Import Question**s (see below). The shared copy must not contain the key.

### 2. Allow only that host

**If** `Vault Base URL` **is** `https://visual-memory-vault-proxy-151358874679.us-east1.run.app/`

- **Text.** Contents: `https://visual-memory-vault-proxy-151358874679.us-east1.run.app`
- **Set Variable.** Variable name `Vault Base URL`, value that text.

**End If**

**If** `Vault Base URL` **is** `https://visual-memory-vault-proxy-151358874679.us-east1.run.app`

- **Nothing**

**Otherwise**

- **Stop and Output**

```text
Not queued
Vault base URL must be https://visual-memory-vault-proxy-151358874679.us-east1.run.app
```

**End If**

A different host, an `http://` URL, a path, or a key in the URL stops here. Nothing is sent. Localhost is for curl, not this shortcut.

**If** `Vault API Key` **does not have any value**

- **Stop and Output**

```text
Not queued
Add the Vault API key to this shortcut. Nothing was sent.
```

**End If**

### 3. Read the share

**Get Images from Input.** Input: **Shortcut Input**. Rename `Shared Images`.

**Count.** Input: `Shared Images`. Rename `Image Count`.

**Get URLs from Input.** Input: **Shortcut Input**. Rename `Shared URLs`.

**Count.** Input: `Shared URLs`. Rename `URL Count`.

**Text.** `Vault Base URL` followed immediately by `/upload`. Rename `Upload URL`.

**Text.** `Vault Base URL` followed immediately by `/capture/stitch`. Rename `Stitch URL`.

**Text.** `Vault Base URL` followed immediately by `/capture/url`. Rename `Page Capture URL`.

### 4. Too many images

**If** `Image Count` **is greater than** `8`

- **Stop and Output**

```text
Not queued
Share 8 images or fewer.
```

**End If**

Stitch's other limits (JPEG, PNG, WebP, or HEIC; 8 MiB and 16000000 pixels per image; stacked size) stay on the proxy. A rejection comes back as `detail`, and step 8 shows it. Pass each image through as the share sheet provided it. Do not add **Convert Image**.

### 5. Two or more images → stitch

There is one **If** per count from 2 through 8. A repeat loop cannot add a variable number of form fields, and this shortcut does not use scripting to build the body.

For each count, the **Get Contents of URL** action is the same except for how many `file` fields it has:

- URL: `Stitch URL`
- Method: **POST**
- Headers: one header, key `X-Api-Key`, value `Vault API Key`
- Request Body: **Form**
- Do not set `Content-Type` yourself. Shortcuts sets the multipart boundary.
- Each form field is named `file`, type **File** (not Text). Values are **Get Item from List** on `Shared Images`, item at index 1, then 2, and so on through that count.

| If Image Count is | `file` fields |
| --- | --- |
| 2 | items 1–2 |
| 3 | items 1–3 |
| 4 | items 1–4 |
| 5 | items 1–5 |
| 6 | items 1–6 |
| 7 | items 1–7 |
| 8 | items 1–8 |

After **Get Contents of URL**, **Set Variable** `Vault Reply` to that result. The two-image case, written out:

**If** `Image Count` **is** `2`

1. **Get Item from List.** List: `Shared Images`. Get **Item at Index** `1`.
2. **Get Item from List.** List: `Shared Images`. Get **Item at Index** `2`.
3. **Get Contents of URL** as specified above, with those two file fields.
4. **Set Variable** `Vault Reply` to the contents.

**End If**

Duplicate that **If** for counts 3 through 8. Add one `file` field per extra image, still named `file`, still type File, in index order. Order is the order iOS hands the images to the share sheet. The proxy stacks from the first field to the last.

### 6. One image → upload

**If** `Image Count` **is** `1`

1. **Get Item from List.** List: `Shared Images`. Get **First Item**.
2. **Get Contents of URL**
   - URL: `Upload URL`
   - Method: **POST**
   - Header: `X-Api-Key` = `Vault API Key`
   - Request Body: **Form**
   - One field, name `file`, type **File**, value: that image
3. **Set Variable** `Vault Reply` to the contents.

**End If**

### 7. URL or Safari page → capture URL

**If** `Image Count` **is** `0`

- **If** `URL Count` **is greater than** `0`
  1. **Get Item from List.** List: `Shared URLs`. Get **First Item**. Rename `Page URL`.
  2. **If** `Page URL` **begins with** `https://`
     - **Nothing**
  3. **Otherwise**
     - **If** `Page URL` **begins with** `http://`
       - **Nothing**
     - **Otherwise**
       - **Stop and Output**

```text
Not queued
That link is not http or https. Nothing was sent.
```

     - **End If**
  4. **End If**
  5. **Dictionary.** One key, `url` (type Text), value `Page URL`.
  6. **Get Contents of URL**
     - URL: `Page Capture URL`
     - Method: **POST**
     - Header: `X-Api-Key` = `Vault API Key`
     - Request Body: **JSON**, set to that dictionary
     - Do not add a `Content-Type` header. Shortcuts sends `application/json`.
  7. **Set Variable** `Vault Reply` to the contents.
- **Otherwise**
  - **Stop and Output**

```text
Not queued
Share a photo, 2 to 8 photos, or a web page.
```

- **End If**

**End If**

### 8. Show queued or not queued

**If** `Vault Reply` **does not have any value**

- **Stop and Output**

```text
Not queued
Vault did not accept this share.
```

**End If**

**Get Dictionary from Input.** Input: `Vault Reply`. If Shortcuts already made the reply a dictionary, use that dictionary for the lookups below.

**Get Dictionary Value.** Key `status`. Rename `Reply Status`.

**Get Dictionary Value.** Key `job_id`. Get it from the same dictionary. Rename `Job ID`.

**Get Dictionary Value.** Key `image_path`. Rename `Image Path`.

Look up `detail` only on the failure path. A queued body has no `detail` key, and fetching a missing key can stop the shortcut before it shows `Queued`.

**If** `Reply Status` **is** `accepted`

- **If** `Job ID` **has any value**
  - **Text**

```text
Queued
job_id: 
```

    then the `Job ID` variable, then a newline, then `image_path: `, then the `Image Path` variable.

  - **Stop and Output** that text.
- **Otherwise**
  - **Stop and Output**

```text
Not queued
Vault did not accept this share.
```

- **End If**

**Otherwise**

- **Get Dictionary Value.** Key `detail`, from the same dictionary. Rename `Error Detail`.
- **If** `Error Detail` **has any value**
  - **Text**

```text
Not queued
```

    then the `Error Detail` variable.

  - **Stop and Output** that text.
- **Otherwise**
  - **Stop and Output**

```text
Not queued
Vault did not accept this share.
```

- **End If**

**End If**

`Get Contents of URL` returns the response body for an HTTP error as well as for `202`. This shortcut treats only `status` `accepted` plus a `job_id` as queued. A proxy `4xx` body is `{"detail":"…"}`, so the result shows that sentence. If the phone cannot reach the proxy, Shortcuts stops on the request itself; that is not a queued job.

`Stop and Output` is the result the share sheet shows. Do not add a wait, a repeat, or a follow-up request after it.

## Publish with Copy iCloud Link

Do this from the phone that should sign the shortcut, after a real share has worked.

1. Open **Share to Vault** in Shortcuts.
2. Clear the API key **Text** action. Leave the base URL text as the live proxy host, or clear it too.
3. Tap the base URL text inside its **Text** action → **Import Question**. Question: `Vault base URL`. Default answer: `https://visual-memory-vault-proxy-151358874679.us-east1.run.app`.
4. Tap the API key **Text** action's text → **Import Question**. Question: `Vault API key (X-Api-Key)`. Leave the default answer empty.
5. Confirm the key is not still written in any action.
6. Tap the share button on the shortcut → **Copy iCloud Link**.

The link is signed by that Apple ID. Anyone who adds it sees the actions first, including the host check, and is asked the two questions once. The answers stay in their copy of the shortcut. They are not part of the link. The next share from Photos or Safari uses the saved answers.

If the key was still in the shortcut when the link was copied, rotate `PROXY_API_KEY`, clear the key, and copy a new link.

Put the new link in this file where the placeholder is, and in the README Mobile Ingestion section.

## Optional poll

Not part of the share. After you have a `job_id`, a terminal or a separate shortcut can check:

```bash
curl "https://visual-memory-vault-proxy-151358874679.us-east1.run.app/jobs/<job_id>" \
  -H "X-Api-Key: <YOUR_PROXY_KEY>"
```

`status` is `pending`, `succeeded`, or `failed`. `succeeded` includes the extract fields. `failed` includes `error`. Do not add this request to **Share to Vault**. The share is done at `202`.

## Check on a phone

- One photo from Photos → result starts with `Queued`. `image_path` is under `/media/` and does not end in `_stitch.jpg` or `_page.jpg`.
- Two to eight photos → `Queued`, and `image_path` ends in `_stitch.jpg`. The proxy stacks them in field order.
- Nine photos → `Not queued` / `Share 8 images or fewer.` No request.
- Safari share on a public page → `Queued`, and `image_path` ends in `_page.jpg`.
- A non-http share with no image → `Not queued` / `That link is not http or https. Nothing was sent.`
- A base URL other than the live proxy → `Not queued` and the host sentence. No request.
- A wrong API key → `Not queued` and the proxy's unauthorized `detail`.
- The result does not contain a receipt summary. That work happens after `202`.
