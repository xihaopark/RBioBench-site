const TASK_CATALOG_URL = 'https://drive.usercontent.google.com/download?id=1eBGbtPN-PsTfkRMqiby1vI8feR4lWx06&export=download';
const FEEDBACK_SHEET_ID = '183OcCpFku-LpbmS7UCQi8TNZw1p9r7olYFQ16PJABa8';
const WEBAPP_URL = 'https://script.google.com/macros/s/AKfycbzXe76o0xg_jTNJ978TC6NtQGO5mKjhNk6p4l_Xr-hfvQVjmcPifB5NWqpEkm4oCx-N/exec';
const SESSION_DAYS = 90;
const SOLUTION_MARKER = '\n\n--- solution.R ---\n';
// Feedback sheet columns (1-based). Columns 1-8 are the original layout; 9-11 were added for reviewer accounts.
const COL = Object.freeze({ created: 1, taskId: 2, track: 3, packageName: 4, name: 5, email: 6, type: 7, comments: 8, reviewId: 9, updated: 10, revision: 11 });
const FEEDBACK_HEADER = ['Timestamp', 'Task', 'Track', 'Package', 'Reviewer', 'Email', 'Feedback type', 'Comments', 'Review ID', 'Last updated', 'Revision'];
const HISTORY_HEADER = ['Archived at', 'Review ID', 'Revision', 'Task', 'Email', 'Feedback type', 'Comments', 'Saved at'];
const ALLOWED_TYPES = ['looks_good', 'needs_change', 'technical_issue'];
const COLAB_URLS = Object.freeze({
  CLIN_0010: 'https://colab.research.google.com/drive/1bgqyCGm6jUmsrRuVOmvwJHyvWkYI_TpV',
  CLIN_0016: 'https://colab.research.google.com/drive/1NyoR3LcOooPFiYbEraP7ilqXfKM8M__s',
  CLIN_0038: 'https://colab.research.google.com/drive/1vJSUzcs-BK0G__XfOc_W7vHbEJcr-iw9',
  CLIN_0043: 'https://colab.research.google.com/drive/1wIMGb0iTWnd6R6905OH32QFZVwPitdF2',
  CLIN_0052: 'https://colab.research.google.com/drive/1uuezYs7O72rA5NEhMuWZP0OjqTwfQnj6',
  BIO_0054: 'https://colab.research.google.com/drive/1UwOHZihOMri0NiRvgY7CXZTppZWFeG57',
  BIO_0137: 'https://colab.research.google.com/drive/152xCrESaOr2OzGB6oZwm0rWinz_zgI3V',
  BIO_0171: 'https://colab.research.google.com/drive/1VneElV_aMZ7v_jVjSA4q4E-o9pq29KIq',
  BIO_0188: 'https://colab.research.google.com/drive/1CJmk7GjWif2K0hPhirV90D9Avib2Wqo8',
  BIO_0203: 'https://colab.research.google.com/drive/1Q3cg_J4DjgioPPiAGP_ZORxePeaSHdpG',
});

function doGet(e) {
  const template = HtmlService.createTemplateFromFile('Index');
  template.loginToken = cleanText_(e && e.parameter && e.parameter.login, 1000).replace(/[^A-Za-z0-9_\-=.]/g, '');
  return template.evaluate()
    .setTitle('RBioBench Task Review')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function include_(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

function getAppData() {
  const response = UrlFetchApp.fetch(TASK_CATALOG_URL, { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) {
    throw new Error('The task catalog is temporarily unavailable.');
  }
  const raw = response.getContentText();
  const tasks = JSON.parse(raw).map(task => Object.assign({}, task, {
    colab_url: COLAB_URLS[task.public_id] || '',
  }));
  return { tasks: tasks };
}

// ---------- Reviewer sign-in (emailed link, stateless signed token) ----------

function requestSignIn(email, name) {
  const cleanEmail = normalizeEmail_(email);
  const cleanName = cleanText_(name, 120);
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(cleanEmail)) throw new Error('Enter a valid email address.');
  if (!cleanName) throw new Error('Enter your name.');
  const token = signToken_({ e: cleanEmail, n: cleanName, x: Date.now() + SESSION_DAYS * 864e5 });
  const link = WEBAPP_URL + '?login=' + encodeURIComponent(token);
  MailApp.sendEmail({
    to: cleanEmail,
    subject: 'Your RBioBench review sign-in link',
    name: 'RBioBench Review',
    htmlBody:
      '<p>Hello ' + escapeHtml_(cleanName) + ',</p>' +
      '<p>Use this link to sign in to the RBioBench task review site. It keeps you signed in for ' + SESSION_DAYS + ' days, ' +
      'and you can use it on any device to see and edit the feedback you have submitted.</p>' +
      '<p><a href="' + link + '">Sign in to RBioBench review</a></p>' +
      '<p>If you did not request this, you can ignore this email.</p>',
  });
  return { ok: true };
}

function verifySession(token) {
  const session = readToken_(token);
  return { email: session.e, name: session.n, expiresAt: session.x };
}

function signToken_(payload) {
  const body = Utilities.base64EncodeWebSafe(JSON.stringify(payload), Utilities.Charset.UTF_8);
  return body + '.' + mac_(body);
}

function readToken_(token) {
  const parts = String(token || '').split('.');
  if (parts.length !== 2 || mac_(parts[0]) !== parts[1]) throw new Error('Please sign in again.');
  const payload = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(parts[0])).getDataAsString('UTF-8'));
  if (!payload.e || !payload.x || payload.x < Date.now()) throw new Error('Your sign-in link has expired. Please request a new one.');
  return payload;
}

function mac_(body) {
  const bytes = Utilities.computeHmacSha256Signature(body, sessionSecret_(), Utilities.Charset.UTF_8);
  return Utilities.base64EncodeWebSafe(bytes).replace(/=+$/, '');
}

function sessionSecret_() {
  const props = PropertiesService.getScriptProperties();
  let secret = props.getProperty('SESSION_SECRET');
  if (!secret) {
    const lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      secret = props.getProperty('SESSION_SECRET');
      if (!secret) {
        secret = Utilities.getUuid() + Utilities.getUuid();
        props.setProperty('SESSION_SECRET', secret);
      }
    } finally {
      lock.releaseLock();
    }
  }
  return secret;
}

// ---------- Feedback ----------

function submitFeedback(payload, token) {
  const session = readToken_(token);
  const clean = {
    reviewId: cleanText_(payload && payload.reviewId, 64),
    taskId: cleanText_(payload && payload.taskId, 32),
    track: cleanText_(payload && payload.track, 32),
    packageName: cleanText_(payload && payload.packageName, 120),
    feedbackType: cleanText_(payload && payload.feedbackType, 32),
    comments: cleanText_(payload && payload.comments, 10000),
    solutionR: cleanText_(payload && payload.solutionR, 50000),
  };
  if (!clean.reviewId && !clean.taskId) throw new Error('Task is required.');
  if (!ALLOWED_TYPES.includes(clean.feedbackType)) throw new Error('Choose a feedback type.');
  const combined = clean.comments + (clean.solutionR ? SOLUTION_MARKER + clean.solutionR : '');
  const now = new Date();

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sheet = feedbackSheet_();
    if (clean.reviewId) {
      const rowIndex = findReviewRow_(sheet, clean.reviewId);
      if (!rowIndex) throw new Error('That review no longer exists.');
      const row = sheet.getRange(rowIndex, 1, 1, FEEDBACK_HEADER.length).getValues()[0];
      if (normalizeEmail_(row[COL.email - 1]) !== session.e) throw new Error('You can only edit your own reviews.');
      const revision = Number(row[COL.revision - 1]) || 1;
      historySheet_().appendRow([now, clean.reviewId, revision, row[COL.taskId - 1], session.e,
        row[COL.type - 1], row[COL.comments - 1], row[COL.updated - 1] || row[COL.created - 1]]);
      sheet.getRange(rowIndex, COL.type, 1, 1).setValue(clean.feedbackType);
      sheet.getRange(rowIndex, COL.comments, 1, 1).setValue(combined);
      sheet.getRange(rowIndex, COL.updated, 1, 2).setValues([[now, revision + 1]]);
      return { ok: true, reviewId: clean.reviewId, taskId: row[COL.taskId - 1], revision: revision + 1 };
    }
    const reviewId = Utilities.getUuid();
    sheet.appendRow([now, clean.taskId, clean.track, clean.packageName, session.n, session.e,
      clean.feedbackType, combined, reviewId, now, 1]);
    return { ok: true, reviewId: reviewId, taskId: clean.taskId, revision: 1 };
  } finally {
    lock.releaseLock();
  }
}

function getMyReviews(token) {
  const session = readToken_(token);
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sheet = feedbackSheet_();
    const last = sheet.getLastRow();
    if (last < 2) return { reviews: [] };
    const rows = sheet.getRange(2, 1, last - 1, FEEDBACK_HEADER.length).getValues();
    const reviews = [];
    rows.forEach((row, i) => {
      if (normalizeEmail_(row[COL.email - 1]) !== session.e) return;
      let reviewId = row[COL.reviewId - 1];
      if (!reviewId) {
        // Rows submitted before reviewer accounts existed get an ID the first time their owner signs in.
        reviewId = Utilities.getUuid();
        sheet.getRange(i + 2, COL.reviewId, 1, 3).setValues([[reviewId, row[COL.created - 1], 1]]);
      }
      const text = String(row[COL.comments - 1] || '');
      const cut = text.indexOf(SOLUTION_MARKER);
      reviews.push({
        reviewId: String(reviewId),
        taskId: String(row[COL.taskId - 1]),
        feedbackType: String(row[COL.type - 1]),
        comments: cut < 0 ? text : text.slice(0, cut),
        solutionR: cut < 0 ? '' : text.slice(cut + SOLUTION_MARKER.length),
        createdAt: toIso_(row[COL.created - 1]),
        updatedAt: toIso_(row[COL.updated - 1] || row[COL.created - 1]),
        revision: Number(row[COL.revision - 1]) || 1,
      });
    });
    reviews.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    return { reviews: reviews };
  } finally {
    lock.releaseLock();
  }
}

function feedbackSheet_() {
  const sheet = SpreadsheetApp.openById(FEEDBACK_SHEET_ID).getSheetByName('Feedback');
  if (!sheet) throw new Error('Feedback sheet is unavailable.');
  const header = sheet.getRange(1, COL.reviewId, 1, 3).getValues()[0];
  if (!header[0]) sheet.getRange(1, COL.reviewId, 1, 3).setValues([FEEDBACK_HEADER.slice(COL.reviewId - 1)]);
  return sheet;
}

function historySheet_() {
  const book = SpreadsheetApp.openById(FEEDBACK_SHEET_ID);
  let sheet = book.getSheetByName('FeedbackHistory');
  if (!sheet) {
    sheet = book.insertSheet('FeedbackHistory');
    sheet.appendRow(HISTORY_HEADER);
  }
  return sheet;
}

function findReviewRow_(sheet, reviewId) {
  const last = sheet.getLastRow();
  if (last < 2) return 0;
  const ids = sheet.getRange(2, COL.reviewId, last - 1, 1).getValues();
  for (let i = 0; i < ids.length; i++) {
    if (String(ids[i][0]) === reviewId) return i + 2;
  }
  return 0;
}

// Run once from the Apps Script editor after deploying, so the owner grants the new Gmail send permission.
function authorizeOnce() {
  sessionSecret_();
  feedbackSheet_();
  Logger.log('Remaining daily email quota: ' + MailApp.getRemainingDailyQuota());
}

function normalizeEmail_(value) {
  return cleanText_(value, 240).toLowerCase();
}

function toIso_(value) {
  return value instanceof Date ? value.toISOString() : String(value || '');
}

function escapeHtml_(value) {
  return String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function cleanText_(value, limit) {
  return String(value == null ? '' : value).trim().slice(0, limit);
}
