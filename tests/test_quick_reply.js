#!/usr/bin/env node
/**
 * Test suite for detectChoiceButtons() in app.js
 * Run: node src/tests/test_quick_reply.js
 */

// ---- Copy of detectChoiceButtons from app.js (lines 804-915) ----
function detectChoiceButtons(text) {
  if (!text || typeof text !== 'string') return null;
  // Strip fenced code blocks before analysis
  const stripped = text.replace(/```[\s\S]*?```/g, '');
  let result = null;

  // --- "Option A/B/C" detection (independent of ? gate) ---
  // Must be at start of line (with optional bold markers) to avoid false positives
  // from numbered list items like "1. Option A".
  const optionRe = /(?:^|\n)\s*\*{0,2}(Option\s+[A-Z](?:\s*[\(\[][^)\]]*[\)\]])?)\*{0,2}/g;
  const optionMatches = [];
  let optMatch;
  while ((optMatch = optionRe.exec(stripped)) !== null) {
    const raw = optMatch[1].trim();
    const letter = raw.match(/Option\s+([A-Z])/);
    if (letter && !optionMatches.some(m => m.letter === letter[1])) {
      optionMatches.push({ label: raw, value: raw, letter: letter[1] });
    }
  }
  if (optionMatches.length >= 2 && optionMatches.length <= 8) {
    result = { options: optionMatches.map(m => ({ label: m.label, value: m.value })) };
  }

  // --- "Restart Server" detection ---
  const _sentences = stripped.split(/(?<=[.!?\n])\s*/);
  const lastSentence = (_sentences.filter(s => s.trim()).pop() || stripped).trim();
  if (!result && (/restart\s+server/i.test(lastSentence) || /server\s+restart/i.test(lastSentence) || (/\brestart\b/i.test(lastSentence) && /\?/.test(lastSentence)))) {
    result = { options: [{ label: 'Restart Server', value: 'Restart Server' }] };
  }

  // --- Numbered list detection ---
  if (!result) {
  const numPatterns = [
    /^\s*\*\*(\d+)[\.\)]\*\*\s+(.+)$/gm,  // **1.** Option
    /^\s*(\d+)\)\s+(.+)$/gm,                // 1) Option
    /^\s*(\d+)\.\s+(.+)$/gm,                // 1. Option
  ];

  const allLines = stripped.split('\n');
  let listGroupCount = 0;
  let inList = false;
  for (const line of allLines) {
    const isListItem = /^\s*(\*\*\d+[\.\)]\*\*\s+|\d+[\.\)]\s+)/.test(line);
    if (isListItem && !inList) {
      listGroupCount++;
      inList = true;
    } else if (!isListItem && line.trim() !== '') {
      inList = false;
    }
  }
  if (listGroupCount > 1) {
    // Multiple separate numbered lists — suppress
  } else {
    for (const re of numPatterns) {
      re.lastIndex = 0;
      const matches = [];
      let match;
      while ((match = re.exec(stripped)) !== null) {
        matches.push({ marker: match[1], value: match[2].trim() });
      }
      if (matches.length < 2 || matches.length > 8) continue;

      result = { options: matches.map(m => ({
        label: m.marker,
        value: m.marker,
      })) };
      break;
    }
  }
  } // end numbered list guard

  // --- Final pass: append Yes button if ? found in last two sentences ---
  // Protect numbered markers (e.g. "1. ", "2. ") from creating false sentence boundaries.
  const sentences = stripped.replace(/\n+/g, ' ')
    .replace(/(\d+)\.\s/g, '$1\u2E31 ')
    .split(/(?<=[.!?])\s+/)
    .map(s => s.replace(/\u2E31/g, '.'))
    .filter(s => s.trim());
  const lastTwo = sentences.slice(-2);
  const endsWithQuestion = /[?？]\s*$/.test(stripped.trim());
  const hasQuestion = endsWithQuestion || lastTwo.some(s => s.includes('?'));
  if (hasQuestion) {
    if (result) {
      if (!result.options.some(o => /^yes/i.test(o.value))) {
        result.options.push({ label: 'Yes, Proceed', value: 'Yes, Proceed' });
      }
    } else {
      result = { options: [{ label: 'Yes, Proceed', value: 'Yes, Proceed' }] };
    }
  }

  return result;
}

// ---- Test Infrastructure ----

let passed = 0;
let failed = 0;
const failures = [];

function test(name, input, expected) {
  const actual = detectChoiceButtons(input);
  const ok = deepEqual(expected, actual);
  const inputPreview = (input || '').substring(0, 100).replace(/\n/g, '\\n');
  if (ok) {
    console.log(`  PASS: ${name}`);
    passed++;
  } else {
    console.log(`  FAIL: ${name}`);
    console.log(`    Input: "${inputPreview}"`);
    console.log(`    Expected: ${JSON.stringify(expected)}`);
    console.log(`    Actual:   ${JSON.stringify(actual)}`);
    failed++;
    failures.push(name);
  }
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (a === null || b === null) return a === b;
  if (typeof a !== typeof b) return false;
  if (typeof a !== 'object') return a === b;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;
  return keysA.every(k => deepEqual(a[k], b[k]));
}

// ---- Test Cases ----

console.log('\n=== SHOULD PRODUCE BUTTONS ===\n');

test(
  'Simple numbered list with ? before',
  'Which do you prefer?\n1. Option one\n2. Option two',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Bold numbered list with ? before',
  'What approach should I take?\n**1.** Refactor the entire module\n**2.** Fix just the broken function\n**3.** Write tests first',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Parenthetical numbered list with ? before',
  'How should we handle this?\n1) Do a full rewrite\n2) Patch it inline\n3) Defer to next sprint',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Option A/B/C with bold',
  'Here are the approaches I see:\n\n**Option A:** Refactor the database layer completely\n**Option B:** Add a caching layer on top\n**Option C:** Optimize the existing queries',
  { options: [
    { label: 'Option A', value: 'Option A' },
    { label: 'Option B', value: 'Option B' },
    { label: 'Option C', value: 'Option C' },
  ] }
);

test(
  'Option A/B with parenthetical qualifiers',
  'I see two paths forward:\n\n**Option A (Recommended):** Use the existing API\n**Option B (More work):** Build a custom solution',
  { options: [
    { label: 'Option A (Recommended)', value: 'Option A (Recommended)' },
    { label: 'Option B (More work)', value: 'Option B (More work)' },
  ] }
);

test(
  'Yes/No question at end',
  'I\'ve analyzed the code and found the issue. The problem is in the auth middleware where tokens aren\'t being refreshed. Want me to fix it?',
  { options: [
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Restart server mention at end',
  'I\'ve made all the changes to server.py and the new endpoint is ready. Should I restart the server?',
  { options: [
    { label: 'Restart Server', value: 'Restart Server' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Restart mention with "restart server" phrasing',
  'Changes are committed. Ready to restart server?',
  { options: [
    { label: 'Restart Server', value: 'Restart Server' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Numbered list + trailing question = numbers + Yes',
  'Which deployment strategy should we use?\n1. Blue-green deployment\n2. Rolling update\n3. Canary release\n\nWant me to implement the one you choose?',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Mixed: numbered options + "Want me to start?"',
  'What should I tackle first?\n1. Fix the login bug\n2. Add the new API endpoint\n3. Update the docs\n\nWant me to start?',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Option A/B with trailing question gets Yes appended',
  'Here are the options:\n\n**Option A:** Quick fix\n**Option B:** Full refactor\n\nWhich do you prefer?',
  { options: [
    { label: 'Option A', value: 'Option A' },
    { label: 'Option B', value: 'Option B' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);


test(
  'Colon before numbered list — "Here are your choices:"',
  'Here are your choices:\n1. Option one\n2. Option two\n3. Option three',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
  ] }
);

test(
  'Colon before numbered list — "I can help with any of these:"',
  'I can help with any of these:\n1. Fix the bug\n2. Add the feature',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
  ] }
);

test(
  'Colon before numbered list — heading label with prose',
  'Here is the output:\n\nThe system processed 500 records.\n\nSteps completed:\n1. Downloaded data\n2. Parsed records\n3. Uploaded results',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
  ] }
);


console.log('\n=== SHOULD NOT PRODUCE BUTTONS (false positive traps) ===\n');

test(
  'Instructional steps with colon — colon gate triggers buttons',
  'Here are the steps to set it up:\n1. Install the package with npm\n2. Run the migration script\n3. Test the connection\n4. Deploy to staging',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: '4', value: '4' },
  ] }
);

test(
  'Multiple separate numbered lists (phase summary)',
  'Here\'s what happened:\n\nPhase 1 completed:\n1. Refactored auth module\n2. Added token refresh\n\nPhase 2 completed:\n1. Updated API routes\n2. Fixed CORS headers\n3. Added rate limiting',
  null
);

test(
  'Numbered items inside code blocks',
  'Run these commands:\n```\n1. npm install\n2. npm run build\n3. npm test\n```\nThat should fix it.',
  null
);

test(
  'Question in paragraph 1, instructional list much later (colon before list triggers)',
  'Did you see the error I mentioned?\n\nAnyway, here\'s the fix. I made three changes to the codebase.\n\nThe implementation follows standard patterns:\n1. Added error handler middleware\n2. Updated the response format\n3. Added logging to track failures\n\nAll tests pass now.',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
  ] }
);

test(
  'Status update with numbered task IDs (colon before list triggers)',
  'Current sprint status:\n1. TASK-101: Completed — auth refactor\n2. TASK-102: In progress — API migration\n3. TASK-103: Not started — UI overhaul\n\nAll on track.',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
  ] }
);

test(
  'Numbered table rows',
  'Results summary:\n\n| # | Status | Commit |\n|---|--------|--------|\n| 1 | Done | abc123 |\n| 2 | Done | def456 |\n| 3 | Pending | — |',
  null
);

test(
  'Does that make sense? after walkthrough = Yes/No only',
  'The middleware chain works like this: first the auth check runs, then the rate limiter, then the actual route handler. Each middleware calls `next()` to pass control. If auth fails, it short-circuits with a 401. Does that make sense?',
  { options: [
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);


console.log('\n=== EDGE CASES ===\n');

test(
  'Single numbered option — should not trigger',
  'What do you think?\n1. Only one option here',
  { options: [
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  '9+ options — should not trigger numbered buttons',
  'Which color?\n1. Red\n2. Blue\n3. Green\n4. Yellow\n5. Purple\n6. Orange\n7. Pink\n8. Teal\n9. Cyan',
  { options: [
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Numbers not starting at 1',
  'Want to pick a phase?\n3. Phase three\n4. Phase four\n5. Phase five',
  { options: [
    { label: '3', value: '3' },
    { label: '4', value: '4' },
    { label: '5', value: '5' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Empty string',
  '',
  null
);

test(
  'Null input',
  null,
  null
);

test(
  'Undefined input',
  undefined,
  null
);

test(
  'Message that is JUST a question',
  'Should I continue?',
  { options: [
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Very long message with small numbered list in middle',
  'I\'ve been working on this for a while now and there are several things to consider. '.repeat(20) +
  '\n\nWhich approach?\n1. Fast path\n2. Safe path\n\n' +
  'Let me know what you think. I can implement either approach relatively quickly. '.repeat(15),
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
  ] }
);

test(
  'Option labels with special markdown chars',
  'Your choices:\n\n**Option A:** Use `async/await` pattern\n**Option B:** Use `.then()` chains',
  { options: [
    { label: 'Option A', value: 'Option A' },
    { label: 'Option B', value: 'Option B' },
  ] }
);

test(
  'Exactly 2 options (minimum)',
  'Which one?\n1. First\n2. Second',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Exactly 8 options (maximum)',
  'Pick a number?\n1. A\n2. B\n3. C\n4. D\n5. E\n6. F\n7. G\n8. H',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: '4', value: '4' },
    { label: '5', value: '5' },
    { label: '6', value: '6' },
    { label: '7', value: '7' },
    { label: '8', value: '8' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);

test(
  'Restart server NOT in last sentence — no restart button',
  'We might need to restart server later. But first, let me finish the code changes.',
  null
);

test(
  'Question in code block should not count as ? gate',
  '```\nWhich one?\n```\n1. Option A\n2. Option B',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
  ] }
);

test(
  'Option A/B inside code block — no buttons',
  'Here is the code:\n```\n**Option A:** foo\n**Option B:** bar\n```\nDone.',
  null
);

test(
  'Numbered list with blank lines between items + ? before',
  'Which do you want?\n\n1. First item\n\n2. Second item\n\n3. Third item',
  { options: [
    { label: '1', value: '1' },
    { label: '2', value: '2' },
    { label: '3', value: '3' },
    { label: 'Yes, Proceed', value: 'Yes, Proceed' },
  ] }
);


// ---- Summary ----
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed out of ${passed + failed} tests`);
if (failures.length > 0) {
  console.log('\nFailing tests:');
  failures.forEach(f => console.log(`  - ${f}`));
}
console.log();
process.exit(failed > 0 ? 1 : 0);
