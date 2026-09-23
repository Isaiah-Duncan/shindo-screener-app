# Setting up the bug-report collector (one-time, ~5 minutes)

The in-app "Report a bug" button in Settings sends reports straight into a
Google Sheet you own, so every user's report lands in one place you can
just open, no server to run or maintain.

## 1. Create the Sheet

1. Go to sheets.google.com and create a new blank spreadsheet.
2. Rename it something like "Shindo Screener: Bug Reports".
3. Rename the first tab (bottom-left) to `Reports`.
4. In row 1, add these headers, one per column (A through H):

   ```
   Timestamp | App Version | Language | Location | Time Format | Platform | User Agent | Description
   ```

## 2. Add the collector script

1. In the Sheet, go to **Extensions → Apps Script**. A new tab opens with
   an empty `Code.gs`.
2. Delete whatever's in there and paste this:

   ```javascript
   function doPost(e) {
     var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Reports");
     var data = JSON.parse(e.postData.contents);

     sheet.appendRow([
       new Date(),
       data.appVersion || "",
       data.language || "",
       data.location || "",
       data.timeFormat || "",
       data.platform || "",
       data.userAgent || "",
       data.description || ""
     ]);

     return ContentService
       .createTextOutput(JSON.stringify({ ok: true }))
       .setMimeType(ContentService.MimeType.JSON);
   }
   ```

3. Click the disk icon (or Ctrl+S) to save the project. Name it anything,
   e.g. "Bug Report Collector".

## 3. Deploy it as a Web App

1. Click **Deploy → New deployment**.
2. Click the gear icon next to "Select type" and choose **Web app**.
3. Set:
   - **Execute as:** Me (your Google account)
   - **Who has access:** Anyone
     (This has to be "Anyone" so the app running on other people's
     computers can reach it with no login. It only *accepts* new rows;
     it doesn't expose your Sheet's contents to the public, since
     `doPost` never reads existing rows back.)
4. Click **Deploy**. The first time, Google will ask you to authorize the
   script: click through the "unverified app" warning (it's your own
   script), since Google shows this for any freshly-written Apps Script.
5. Copy the **Web app URL** it gives you. It looks like:
   `https://script.google.com/macros/s/AKfycb.../exec`

## 4. Wire it into the app

In `screener_app.html`, find this line near the top of the `<script>` block:

```javascript
const BUG_REPORT_ENDPOINT = "PASTE_YOUR_APPS_SCRIPT_WEB_APP_URL_HERE";
```

Replace the placeholder string with the Web app URL from step 3, save,
and rebuild the `.exe` (see BUILD.md for the command). Since the build
uses `--onefile`, `screener_app.html` gets bundled into the `.exe`
itself rather than read from a file next to it, so a rebuild is needed
for this change to actually take effect there; running `python app.py`
directly picks it up immediately without a rebuild, since that reads
the file straight from disk.

## 5. Test it

Open the app, go to Settings → Support, type anything in the box, and
click **Submit report**. You should see "Sent, thank you." in the app,
and a new row should appear in your Sheet within a few seconds.

## Notes

- If you ever need to redeploy the script after editing it (e.g. adding a
  column), use **Deploy → Manage deployments → edit (pencil) → New
  version**, not "New deployment": that keeps the same URL, so you won't
  need to update the app again.
- The "Open email app" and "Copy text" buttons stay as fallbacks below
  the Submit button, so a report can still reach you even if someone's
  offline, has a strict firewall, or the endpoint is ever down.
- This is a free Google Apps Script quota (Google's limits are generous
  for this volume: ~20,000 URL Fetch/web app calls per day on a personal
  account), so there's no cost concern at beta-tester scale.
