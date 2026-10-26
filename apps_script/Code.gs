/**
 * Telegram News Collector receiver.
 *
 * Required Script Properties:
 *   COLLECTOR_SECRET  - the same random secret as on Bothost
 *   SPREADSHEET_ID    - destination spreadsheet ID
 *
 * Managed columns must be the first columns on both destination sheets.
 * User-editable columns may be added to the right; this script never writes them.
 */

const MAX_BATCH_SIZE = 100;
const ALL_NEWS_SHEET = 'Все новости';
const TAGGED_SHEET = 'ИЖС-КОРП';
const MANAGED_HEADERS = [
  'record_key',
  'Дата сообщения',
  'Группа',
  'Отправитель',
  'Источник пересылки',
  'Текст новости',
  'Ссылки',
  'Хештеги',
  'ID сообщения',
  'ID группы',
  'Дата сбора'
];


function doPost(e) {
  let request;
  try {
    request = JSON.parse((e && e.postData && e.postData.contents) || '');
  } catch (error) {
    return jsonResponse_({ok: false, error: 'invalid_json'});
  }

  const properties = PropertiesService.getScriptProperties();
  const expectedSecret = properties.getProperty('COLLECTOR_SECRET');
  if (!expectedSecret || request.secret !== expectedSecret) {
    return jsonResponse_({ok: false, error: 'unauthorized'});
  }
  if (request.version !== 1) {
    return jsonResponse_({ok: false, error: 'unsupported_version'});
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return jsonResponse_({ok: false, error: 'busy'});
  }
  try {
    if (request.action === 'sync') {
      return jsonResponse_(syncRecords_(request.records));
    }
    if (request.action === 'repair') {
      return jsonResponse_(repairSheets_());
    }
    return jsonResponse_({ok: false, error: 'unsupported_action'});
  } catch (error) {
    return jsonResponse_({ok: false, error: safeError_(error)});
  } finally {
    lock.releaseLock();
  }
}


function syncRecords_(records) {
  if (!Array.isArray(records) || records.length < 1) {
    return {ok: false, error: 'records_must_be_non_empty_array'};
  }
  if (records.length > MAX_BATCH_SIZE) {
    return {ok: false, error: 'batch_too_large'};
  }

  const validated = records.map(validateRecord_);
  const requestKeys = new Set();
  validated.forEach(function(record) {
    if (requestKeys.has(record.record_key)) throw new Error('duplicate_record_key_in_batch');
    requestKeys.add(record.record_key);
  });
  const sheets = getSheets_();
  const allKeys = existingKeys_(sheets.allNews);
  const taggedKeys = existingKeys_(sheets.tagged);
  const results = {};
  const allRows = [];
  const taggedRows = [];

  validated.forEach(function(record) {
    const result = {
      record_key: record.record_key,
      all_news: allKeys.has(record.record_key) ? 'exists' : 'pending',
      izhs_corp: record.hashtags.length
        ? (taggedKeys.has(record.record_key) ? 'exists' : 'pending')
        : 'not_required'
    };
    results[record.record_key] = result;
    if (result.all_news === 'pending') {
      allKeys.add(record.record_key);
      allRows.push({record: record, result: result});
    }
    if (result.izhs_corp === 'pending') {
      taggedKeys.add(record.record_key);
      taggedRows.push({record: record, result: result});
    }
  });

  appendGroup_(sheets.allNews, allRows, 'all_news');
  appendGroup_(sheets.tagged, taggedRows, 'izhs_corp');

  return {
    ok: true,
    request_count: validated.length,
    results: validated.map(function(record) { return results[record.record_key]; })
  };
}


function appendGroup_(sheet, items, resultField) {
  if (!items.length) return;
  try {
    appendRows_(sheet, items.map(function(item) { return rowFromRecord_(item.record); }));
    items.forEach(function(item) { item.result[resultField] = 'inserted'; });
  } catch (error) {
    const reason = safeError_(error);
    items.forEach(function(item) {
      item.result[resultField] = 'error';
      item.result.error = reason;
    });
  }
}


function repairSheets_() {
  const sheets = getSheets_();
  const allRecords = recordsFromSheet_(sheets.allNews);
  const taggedRecords = recordsFromSheet_(sheets.tagged);
  const allKeys = new Set(allRecords.map(function(record) { return record.record_key; }));
  const taggedKeys = new Set(taggedRecords.map(function(record) { return record.record_key; }));

  const missingAll = taggedRecords.filter(function(record) {
    return record.record_key && !allKeys.has(record.record_key);
  });
  const missingTagged = allRecords.filter(function(record) {
    return record.record_key && record.hashtags.length && !taggedKeys.has(record.record_key);
  });

  appendRows_(sheets.allNews, missingAll.map(rowFromRecord_));
  appendRows_(sheets.tagged, missingTagged.map(rowFromRecord_));
  return {
    ok: true,
    all_news_repaired: missingAll.length,
    izhs_corp_repaired: missingTagged.length
  };
}


function getSheets_() {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!spreadsheetId) throw new Error('missing_spreadsheet_id');
  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  const allNews = spreadsheet.getSheetByName(ALL_NEWS_SHEET);
  const tagged = spreadsheet.getSheetByName(TAGGED_SHEET);
  if (!allNews || !tagged) throw new Error('required_sheet_missing');
  assertHeaders_(allNews);
  assertHeaders_(tagged);
  return {allNews: allNews, tagged: tagged};
}


function assertHeaders_(sheet) {
  const actual = sheet.getRange(1, 1, 1, MANAGED_HEADERS.length).getValues()[0];
  MANAGED_HEADERS.forEach(function(header, index) {
    if (actual[index] !== header) {
      throw new Error('invalid_headers_' + sheet.getName());
    }
  });
}


function existingKeys_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return new Set();
  const values = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  return new Set(values.map(function(row) {
    return normalizedKey_(row[0]);
  }).filter(Boolean));
}


function appendRows_(sheet, rows) {
  if (!rows.length) return;
  const firstRow = Math.max(2, sheet.getLastRow() + 1);
  sheet.getRange(firstRow, 1, rows.length, MANAGED_HEADERS.length).setValues(rows);
}


function validateRecord_(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    throw new Error('invalid_record');
  }
  if (!/^-?\d+:\d+$/.test(String(record.record_key || ''))) {
    throw new Error('invalid_record_key');
  }
  if (!Array.isArray(record.links) || !Array.isArray(record.hashtags)) {
    throw new Error('links_and_hashtags_must_be_arrays');
  }
  const allowedTags = new Set(['#ижс', '#корп']);
  record.hashtags.forEach(function(tag) {
    if (!allowedTags.has(String(tag).toLowerCase())) throw new Error('invalid_hashtag');
  });
  const normalized = {
    record_key: limitedString_(record.record_key, 128),
    message_date: limitedString_(record.message_date, 128),
    group_title: limitedString_(record.group_title, 1000),
    sender_name: limitedString_(record.sender_name, 1000),
    forward_source: limitedString_(record.forward_source, 2000),
    text: limitedString_(record.text, 32767),
    links: record.links.map(function(value) { return limitedString_(value, 4096); }),
    hashtags: record.hashtags.map(function(value) { return String(value).toLowerCase(); }),
    message_id: limitedString_(record.message_id, 128),
    chat_id: limitedString_(record.chat_id, 128),
    collected_at: limitedString_(record.collected_at, 128)
  };
  return normalized;
}


function limitedString_(value, maxLength) {
  return String(value === null || value === undefined ? '' : value).slice(0, maxLength);
}


function safeCell_(value) {
  const text = String(value === null || value === undefined ? '' : value);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}


function normalizedKey_(value) {
  return String(value || '').replace(/^'/, '');
}


function rowFromRecord_(record) {
  return [
    safeCell_(record.record_key),
    safeCell_(record.message_date),
    safeCell_(record.group_title),
    safeCell_(record.sender_name),
    safeCell_(record.forward_source),
    safeCell_(record.text),
    safeCell_(record.links.join('\n')),
    safeCell_(record.hashtags.join(', ')),
    safeCell_(record.message_id),
    safeCell_(record.chat_id),
    safeCell_(record.collected_at)
  ];
}


function recordsFromSheet_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  return sheet
    .getRange(2, 1, lastRow - 1, MANAGED_HEADERS.length)
    .getValues()
    .filter(function(row) { return row[0]; })
    .map(function(row) {
      return {
        record_key: normalizedKey_(row[0]),
        message_date: String(row[1] || ''),
        group_title: String(row[2] || ''),
        sender_name: String(row[3] || ''),
        forward_source: String(row[4] || ''),
        text: String(row[5] || ''),
        links: String(row[6] || '').split('\n').filter(Boolean),
        hashtags: String(row[7] || '').split(',').map(function(tag) {
          return tag.trim().toLowerCase();
        }).filter(function(tag) { return tag === '#ижс' || tag === '#корп'; }),
        message_id: String(row[8] || ''),
        chat_id: String(row[9] || ''),
        collected_at: String(row[10] || '')
      };
    });
}


function safeError_(error) {
  const message = error && error.message ? String(error.message) : 'internal_error';
  return message.slice(0, 200);
}


function jsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

