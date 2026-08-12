Put the two Firebase ADMIN SDK service account JSON files here:

  white-force-jobs-service-account.json
  white-force-attendance-service-account.json

How to get them (do this once per Firebase project):
  1. Go to https://console.firebase.google.com/
  2. Open the project ("white-force-e7ee2" for Jobs, "white-force-attendance-app" for Attendance)
  3. Gear icon -> Project settings -> Service accounts tab
  4. Click "Generate new private key" -> a JSON file downloads
  5. Rename it to match the filenames above and drop it in this folder

IMPORTANT: This is a DIFFERENT file from google-services.json.
google-services.json only configures the Android client; it has no
authority to send messages from a server. The service account JSON is
the one that lets Django talk to FCM on the project's behalf, so never
commit it to git or share it outside the team.
