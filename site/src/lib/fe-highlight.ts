// A small Fe highlighter for the playground, which edits and shows code in the browser where
// Shiki would be too heavy. It follows site/src/lib/fe.tmLanguage.json, line by line, and uses
// the same color tokens.

const CONTROL = new Set(['if', 'else', 'match', 'for', 'in', 'while', 'loop', 'return', 'break', 'continue']);
const STORAGE = new Set([
  'fn', 'struct', 'enum', 'trait', 'impl', 'type', 'const', 'contract', 'msg', 'recv', 'init', 'mod',
  'use', 'extern', 'static_assert', 'pub', 'let', 'var', 'mut', 'ref', 'own', 'uses', 'with', 'where',
  'as', 'unsafe', 'ingot', 'super', 'self', 'Self',
]);
const PRIMITIVE = /^(bool|u8|u16|u32|u64|u128|u256|i8|i16|i32|i64|i128|i256|usize|isize)$/;

const TOKEN =
  /(\/\/.*)|(#!?\[[^\]\n]*\]?)|("(?:[^"\\\n]|\\.)*"?|b'(?:[^'\\\n]|\\.)*')|(\b(?:0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|\d[\d_]*)\b)|([A-Za-z_][A-Za-z0-9_]*!?)|(::|->|=>|[{}()[\],;:.<>=+\-*/&|!?%^~])/g;

export function escape(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function word(text: string, rest: string): string {
  if (text.endsWith('!')) return 'fn';
  if (CONTROL.has(text) || STORAGE.has(text)) return 'kw';
  if (text === 'true' || text === 'false' || PRIMITIVE.test(text)) return 'const';
  if (/^[A-Z]/.test(text)) return 'ty';
  if (/^\s*(<[^()]*>)?\s*\(/.test(rest)) return 'fn';
  return '';
}

/** One line of Fe as HTML with token spans. */
export function highlightLine(line: string): string {
  let out = '';
  let last = 0;
  for (const match of line.matchAll(TOKEN)) {
    const index = match.index ?? 0;
    out += escape(line.slice(last, index));
    const [text, comment, attribute, string, number, ident] = match;
    let kind = 'punct';
    if (comment) kind = 'comment';
    else if (attribute) kind = 'attr';
    else if (string) kind = 'str';
    else if (number) kind = 'const';
    else if (ident) kind = word(ident, line.slice(index + text.length));
    out += kind ? `<span class="tk-${kind}">${escape(text)}</span>` : escape(text);
    last = index + text.length;
  }
  return out + escape(line.slice(last));
}

/** Fe source as HTML, one block per line, with `errorLine` (1-based) marked. */
export function highlight(source: string, errorLine = 0): string {
  return source
    .split('\n')
    .map((line, i) => {
      const body = highlightLine(line) || '​';
      return `<span class="line${i + 1 === errorLine ? ' error' : ''}">${body}</span>`;
    })
    .join('');
}
