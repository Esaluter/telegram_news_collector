const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class Range {
  constructor(sheet, row, col, rows, cols) {
    this.sheet = sheet; this.row = row; this.col = col; this.rows = rows; this.cols = cols;
  }
  getValues() {
    const values = [];
    for (let r = 0; r < this.rows; r++) {
      const row = [];
      for (let c = 0; c < this.cols; c++) {
        let value = (this.sheet.rows[this.row - 1 + r] || [])[this.col - 1 + c] || '';
        // Google Sheets uses the leading apostrophe as an input marker and does
        // not return it from getValues().
        if (typeof value === 'string' && value.startsWith("'")) value = value.slice(1);
        row.push(value);
      }
      values.push(row);
    }
    return values;
  }
  setValues(values) {
    if (this.sheet.failNextWrite) {
      this.sheet.failNextWrite = false;
      throw new Error('simulated_write_failure');
    }
    for (let r = 0; r < values.length; r++) {
      while (this.sheet.rows.length < this.row + r) this.sheet.rows.push([]);
      for (let c = 0; c < values[r].length; c++) {
        this.sheet.rows[this.row - 1 + r][this.col - 1 + c] = values[r][c];
      }
    }
  }
}

class Sheet {
  constructor(name, headers) { this.name = name; this.rows = [headers.slice()]; this.failNextWrite = false; }
  getName() { return this.name; }
  getLastRow() { return this.rows.length; }
  getRange(row, col, rows, cols) { return new Range(this, row, col, rows, cols); }
}

const headers = [
  'record_key','Дата сообщения','Группа','Отправитель','Источник пересылки',
  'Текст новости','Ссылки','Хештеги','ID сообщения','ID группы','Дата сбора'
];
const sheets = {
  'Все новости': new Sheet('Все новости', headers),
  'ИЖС-КОРП': new Sheet('ИЖС-КОРП', headers)
};
const properties = {COLLECTOR_SECRET: 'secret', SPREADSHEET_ID: 'sheet-id'};

const context = {
  console,
  PropertiesService: {getScriptProperties: () => ({getProperty: key => properties[key]})},
  SpreadsheetApp: {openById: () => ({getSheetByName: name => sheets[name]})},
  LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock: () => {}})},
  ContentService: {
    MimeType: {JSON: 'json'},
    createTextOutput: text => ({text, setMimeType() { return this; }})
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('apps_script/Code.gs', 'utf8'), context);

function request(body) {
  const output = context.doPost({postData: {contents: JSON.stringify(body)}});
  return JSON.parse(output.text);
}
function payload(key, hashtags) {
  const [chatId, messageId] = key.split(':');
  return {
    record_key: key, message_date: '2026-01-01T00:00:00Z', group_title: 'Группа',
    sender_name: 'Имя', forward_source: '', text: '=формула', links: ['https://example.com'],
    hashtags, message_id: messageId, chat_id: chatId, collected_at: '2026-01-01T00:00:01Z'
  };
}

assert.equal(request({version: 1, action: 'sync', secret: 'wrong', records: [payload('-1:1', [])]}).error, 'unauthorized');
let first = request({version: 1, action: 'sync', secret: 'secret', records: [payload('-1:1', []), payload('-1:2', ['#ижс'])]});
assert.equal(first.ok, true);
assert.equal(sheets['Все новости'].rows.length, 3);
assert.equal(sheets['ИЖС-КОРП'].rows.length, 2);
assert.equal(sheets['Все новости'].rows[1][5], "'=формула");

sheets['Все новости'].rows[2][11] = 'ручная правка';
let repeated = request({version: 1, action: 'sync', secret: 'secret', records: [payload('-1:2', ['#ижс'])]});
assert.equal(repeated.results[0].all_news, 'exists');
assert.equal(repeated.results[0].izhs_corp, 'exists');
assert.equal(sheets['Все новости'].rows[2][11], 'ручная правка');

sheets['ИЖС-КОРП'].failNextWrite = true;
let partial = request({version: 1, action: 'sync', secret: 'secret', records: [payload('-1:3', ['#корп'])]});
assert.equal(partial.results[0].all_news, 'inserted');
assert.equal(partial.results[0].izhs_corp, 'error');
let retry = request({version: 1, action: 'sync', secret: 'secret', records: [payload('-1:3', ['#корп'])]});
assert.equal(retry.results[0].all_news, 'exists');
assert.equal(retry.results[0].izhs_corp, 'inserted');

sheets['ИЖС-КОРП'].rows.pop();
let repair = request({version: 1, action: 'repair', secret: 'secret'});
assert.equal(repair.izhs_corp_repaired, 1);
assert.equal(sheets['ИЖС-КОРП'].rows.length, 3);
console.log('Apps Script offline tests: OK');

