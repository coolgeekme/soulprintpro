/**
 * Intent Detector — Analyzes user messages to determine what type of AI action is needed.
 * 
 * Returns: { type, subtype, confidence, patterns_matched }
 * 
 * Types:
 *   'chat'       — text conversation (question, task, creative writing, etc.)
 *   'image'      — image generation request
 *   'video'      — video generation request
 *   'music'      — music / song / jingle generation request
 *   'image_edit' — edit/modify an existing image
 *   'refused'    — user is complaining about unwanted AI actions
 * 
 * Subtypes:
 *   chat:       'question' | 'task' | 'creative' | 'code' | 'analysis' | 'web_search' | 'general'
 *   image:      'infographic' | 'logo' | 'photo' | 'art' | 'diagram' | 'general'
 *   video:      'cinematic' | 'talking' | 'dance' | 'animation' | 'general'
 *   music:      'instrumental' | 'jingle' | 'lyrics' | 'soundtrack' | 'general'
 *   image_edit: 'aspect_ratio' | 'mask_edit' | 'style_change' | 'general'
 *
 * Zero dependencies. Pure JavaScript.
 */

// ═══════════════════════════════════════════════════════════════════════════════
// QUESTION DETECTION
// ═══════════════════════════════════════════════════════════════════════════════

const QUESTION_PATTERNS = [
  // Starts with a question word
  /^(?:why|what|how|when|where|who|which|is|are|do|does|did|can|could|would|should|will|shall|have|has|had|was|were|isn't|aren't|don't|doesn't|didn't|wasn't|weren't|won't|wouldn't|couldn't|shouldn't|tell me|explain|describe|clarify|elaborate)\b/i,
  // Ends with question mark
  /\?\s*$/,
  // Explicitly says "question" or "asking"
  /\b(?:question|i'?m asking|not an edit|just asking|i have a question|asking a question)\b/i,
  // "In this [concept], why/what/how..." pattern
  /\bin this\b.*\b(?:why|what|how|who)\b/i,
  // "Why is/are/does..." — clearly a question even without "?"
  /^why\s+(?:is|are|does|do|did|was|were|would|should|can|could)\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// ANTI-EDIT DETECTION (user complaining about unwanted actions)
// ═══════════════════════════════════════════════════════════════════════════════

const REFUSAL_PATTERNS = [
  /\bstop\b.*\b(making|generating|creating|editing|changing|modifying|producing|doing)\b/i,
  /\b(?:don'?t|do\s+not|please\s+don'?t|stop)\b.*\b(edit|change|modify|alter|make|generate|create)\b/i,
  /\bno\s+more\b.*\b(image|picture|photo|edit|change|generation|modification)\b/i,
  /\bi\s+(?:did\s*n[o']?t|didn'?t|never)\s+(?:ask|request|want)\b/i,
  /\b(unwanted|unasked|unsolicited|without\s+(?:my\s+)?(?:permission|asking|request))\b/i,
  /\byou\s+keep\b.*\b(generat|edit|chang|mak|creat|modif)\w*/i,
  /\bthat'?s?\s+not\s+what\s+i\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// VIDEO INTENT
// ═══════════════════════════════════════════════════════════════════════════════

const VIDEO_PATTERNS = [
  /^(?:please\s+)?(?:can you\s+)?(?:generate|create|make)\s+(?:a\s+)?(?:[\w]+\s+)*video\b/i,
  /^(?:please\s+)?(?:can you\s+)?video\s+of\b/i,
  /^(?:please\s+)?(?:can you\s+)?animate\b/i,
  /\bturn\s+(?:this|it)\s+into\s+(?:a\s+)?(?:video|animation|clip)\b/i,
  /\bmake\s+(?:this|it)\s+(?:a\s+|into\s+(?:a\s+)?)?(?:video|animation)\b/i,
  /\bconvert\s+(?:this|it)?\s*(?:to|into)\s+(?:a\s+)?(?:video|animation)\b/i,
  /\bi\s+want\s+(?:a\s+|an\s+)?(?:[\w]+\s+)*(?:video|animation)\b/i,
  /\b(?:create|make|generate)\s+(?:a\s+)?(?:[\w]+\s+)*video\s+(?:from|of|with|using|about|showing)\b/i,
  /\b(?:animate|bring\s+to\s+life)\s+(?:this|it|the|my|that)\b/i,
  /\bvideo\s+(?:from|of)\s+(?:this|the|my|that)\b/i,
  /\b(?:make|let)\s+(?:it|this|that)\s+(?:move|drive|fly|walk|run|spin|dance|swim|jump|bounce|rotate|float)\b/i,
  /\banimated\s+(?:version|clip|vid)\b/i,
  /\bcreate\s+(?:a\s+|an\s+)?animation\b/i,
  /\bvideo\s+prompt\s+(?:for|of|about|with|showing)\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// INFOGRAPHIC / FLYER INTENT
// ═══════════════════════════════════════════════════════════════════════════════

const INFOGRAPHIC_PATTERNS = [
  /(?:please\s+)?(?:can you\s+)?(?:generate|create|make|design|build)\s+(?:an?\s+)?(?:beautiful\s+)?infographic/i,
  /(?:please\s+)?(?:can you\s+)?(?:generate|create|make|design|build)\s+(?:a\s+)?(?:beautiful\s+)?flyer/i,
  /(?:please\s+)?(?:can you\s+)?(?:generate|create|make|design|build)\s+(?:a\s+)?(?:beautiful\s+)?poster/i,
  /(?:please\s+)?(?:can you\s+)?(?:generate|create|make|design|build)\s+(?:a\s+)?(?:beautiful\s+)?banner/i,
  /(?:please\s+)?(?:can you\s+)?(?:generate|create|make|design|build)\s+(?:a\s+)?(?:beautiful\s+)?brochure/i,
  /(?:turn|convert)\s+(?:this|it)\s+into\s+(?:an?\s+)?(?:infographic|flyer|poster)/i,
  /(?:i\s+)?(?:want|need|would like)\s+(?:an?\s+)?(?:infographic|flyer|poster)/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// IMAGE INTENT
// ═══════════════════════════════════════════════════════════════════════════════

const IMAGE_PATTERNS = [
  /^(?:please\s+)?(?:can you\s+)?(?:generate|create|make)\s+(?:me\s+)?(?:an?|the)\s+(?:[\w:./-]+\s+){0,4}(?:image|picture|photo|illustration|art|artwork|graphic|visual|diagram|chart|schematic|blueprint|flowchart|wireframe|mindmap)\b/i,
  /^(?:please\s+)?(?:can you\s+)?(?:draw|paint|sketch)\s+(?:me\s+)?(?:a|an)\s+/i,
  /^(?:please\s+)?(?:can you\s+)?illustrate\s+/i,
  /^(?:please\s+)?(?:can you\s+)?(?:show|give)\s+me\s+(?:an?\s+)?(?:picture|image|photo|illustration)\b/i,
  /^(?:please\s+)?(?:can you\s+)?design\s+(?:me\s+)?(?:a|an|the|my)\s+(?:logo|icon|banner|header|thumbnail|cover|graphic|badge|emblem|mascot|avatar|wallpaper|background|mockup)\b/i,
  /^(?:please\s+)?(?:can you\s+)?visualize\s+/i,
  /(?:i\s+)?(?:want|need|would like)\s+(?:an?\s+)?(?:image|picture|photo|illustration|logo|icon|graphic|drawing|sketch|portrait|painting|render|diagram|chart)\s+(?:of|for|with|showing)\b/i,
  /^(?:generate|create|make)\s+(?:the\s+)?(?:image|picture|logo|design|visual|graphic|diagram|chart)\b/i,
  /\bdall-?e\b/i, /\bstable\s+diffusion\b/i, /\bmidjourney\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// IMAGE EDIT INTENT
// ═══════════════════════════════════════════════════════════════════════════════

const ASPECT_RATIO_PATTERNS = [
  /\b(recreate|re-create|regenerate|redo|remake)\b.*\b(\d+\s*[:x×]\s*\d+|portrait|landscape|square|vertical|horizontal|widescreen|cinematic)\b/i,
  /\b(make|convert|turn|switch)\b\s+(?:this|it|that)\s+(?:a\s+)?(?:into\s+(?:a\s+)?)?(\d+\s*[:x×]\s*\d+|portrait|landscape|square|vertical|horizontal|widescreen|cinematic)\b/i,
  /\b(change|switch|convert|modify|update)\b.*\b(aspect\s*ratio|ratio|dimensions?|format|orientation)\b.*\b(to|into)\s+/i,
  /\b(fit|adapt|resize|reformat|adjust)\b\s+(?:this|it|that)\s+(?:to|for|into)\s+/i,
];

const EDIT_PATTERNS = [
  /\b(edit|modify|change|alter|adjust|fix|update|improve|enhance|tweak|replace|swap|remove|add|put)\b.*\b(image|picture|photo|pic|graphic|illustration)\b/i,
  /\b(image|picture|photo|pic|graphic|illustration)\b.*\b(edit|modify|change|alter|adjust|fix|update|improve|enhance|tweak)\b/i,
  /\b(make|change)\s+(the|this|that|it)\b.*\b(brighter|darker|bigger|smaller|bluer|redder|warmer|cooler)\b/i,
  /\b(remove|delete|erase|get rid of)\b.*\b(from|in)\b.*\b(image|picture|photo|background)\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// MUSIC INTENT
// ═══════════════════════════════════════════════════════════════════════════════
// Order note: the `MUSIC_PATTERNS` are checked AFTER `VIDEO_PATTERNS` in
// `detectIntent`, so a phrase like "create a music video" still routes to
// video (the video patterns include `music\s*video` in the dance subtype).
// Pure music asks (no video keyword) fall through to here cleanly.

const MUSIC_PATTERNS = [
  // Verb + musical noun: "compose me a song", "write me a jingle", "produce a
  // soundtrack". Allows up to 4 modifier words between the verb and the noun
  // ("make me a calm lo-fi background track").
  /^(?:please\s+)?(?:can you\s+)?(?:compose|generate|create|make|produce|build|craft|write\s+me|write|record|give\s+me|play)\s+(?:me\s+)?(?:a|an|the|some)?\s*(?:[\w-]+\s+){0,4}(?:song|songs|jingle|jingles|track|tracks|tune|tunes|melody|melodies|soundtrack|theme(?:\s+song|\s+tune)?|score|anthem|beat|beats|loop|riff|hymn|ballad|composition|background\s+music|instrumental(?:\s+track)?|vocal(?:\s+track)?)\b/i,
  // Music style + musical noun: "lo-fi hip hop track", "edm beat", "jazz piano loop"
  /\b(?:lo-?fi|hip\s*hop|edm|techno|house|jazz|classical|rock|pop|country|metal|indie|ambient|orchestral|trap|reggae|blues|funk|disco|punk|folk|chiptune|synthwave|vaporwave|cinematic|epic|electronic|acoustic|piano)\s+(?:song|track|beat|loop|tune|music|instrumental|score|melody)\b/i,
  // Purpose-built music: "background music for my podcast", "intro music"
  /\b(?:background|theme|intro|outro|elevator|jingle|podcast|ad|advert|commercial)\s+music\b/i,
  // Song lyrics specifically — Suno generates these along with audio
  /^(?:please\s+)?(?:can you\s+)?(?:write|create|generate|compose|make)\s+(?:me\s+)?(?:a|the|some)?\s*(?:song\s+)?lyrics\b/i,
  // "I want / need a song / track / jingle / soundtrack"
  /(?:i\s+)?(?:want|need|would\s+like)\s+(?:a|an|some)\s+(?:[\w-]+\s+){0,3}(?:song|track|jingle|tune|beat|loop|melody|soundtrack|theme|anthem|composition|instrumental|background\s+music)\b/i,
  // "Instrumental for X" / "vocal for X"
  /^(?:please\s+)?(?:can you\s+)?(?:generate|create|make|produce|compose)\s+(?:an?\s+)?(?:instrumental|vocal)\b/i,
  // Suno mentioned by name
  /\bsuno\b/i,
];

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT SUBTYPE DETECTION
// ═══════════════════════════════════════════════════════════════════════════════

const CHAT_SUBTYPES = {
  code: /\b(code|function|class|api|debug|error|bug|compile|syntax|regex|sql|html|css|python|javascript|typescript|react|node|git|deploy)\b/i,
  creative: /\b(write|poem|story|song|lyrics|script|essay|letter|speech|novel|fiction|creative|compose|draft)\b/i,
  analysis: /\b(analyze|analysis|evaluate|compare|assess|review|critique|break\s*down|pros\s+and\s+cons|summarize|summary)\b/i,
  web_search: /\b(search|look\s+up|find|latest|current|today|news|weather|score|price|stock|trending|recent|2025|2026)\b/i,
  task: /\b(task|todo|list|plan|schedule|organize|prioritize|steps|roadmap|checklist|workflow)\b/i,
  math: /\b(calculate|compute|solve|equation|formula|derivative|integral|probability|statistics|algebra|geometry)\b/i,
};

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN DETECT FUNCTION
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Detect the intent behind a user message.
 * 
 * @param {string} message - The user's input text
 * @param {object} [context] - Optional context
 * @param {boolean} [context.hasImageAttachment] - Whether the message includes an image
 * @param {string|null} [context.lastImageUrl] - URL of the last image in conversation
 * @param {boolean} [context.hasVideoAttachment] - Whether the message includes a video
 * @returns {{ type: string, subtype: string|null, confidence: string, metadata?: object }}
 */
export function detectIntent(message, context = {}) {
  if (!message || typeof message !== 'string') {
    return { type: 'chat', subtype: 'general', confidence: 'default' };
  }

  const text = message.trim();
  const lower = text.toLowerCase();

  // Guard: skip detection for very long texts (likely documents, not commands)
  if (text.length > 800) {
    return { type: 'chat', subtype: detectChatSubtype(lower), confidence: 'default' };
  }

  // Guard: skip if text has many line breaks (likely a list/document)
  if ((text.match(/\n/g) || []).length > 5) {
    return { type: 'chat', subtype: detectChatSubtype(lower), confidence: 'default' };
  }

  // 1. Check for refusal/complaint (highest priority)
  if (REFUSAL_PATTERNS.some(p => p.test(text))) {
    return { type: 'refused', subtype: null, confidence: 'high' };
  }

  // 2. Check if it's clearly a question
  const isQuestion = QUESTION_PATTERNS.some(p => p.test(text));

  // 3. Video detection (more specific, check before image)
  if (!isQuestion && VIDEO_PATTERNS.some(p => p.test(lower))) {
    return { type: 'video', subtype: detectVideoSubtype(lower), confidence: 'high' };
  }

  // 3b. Force video if image attachment + video keywords
  if (!isQuestion && context.hasImageAttachment) {
    if (/\b(video|animate|animation|animated|motion|bring\s+to\s+life|clip)\b/i.test(lower)) {
      return { type: 'video', subtype: 'animation', confidence: 'high' };
    }
  }

  // 3c. Animate keywords with existing image context
  if (!isQuestion && /\b(animate|animation|animated|bring\s+to\s+life)\b/i.test(lower)) {
    return { type: 'video', subtype: 'animation', confidence: 'medium' };
  }

  // 3d. Music detection — runs AFTER video so "music video" still wins as
  // video. Pure music asks ("compose me a jingle", "make a lo-fi beat",
  // "write me a song about my dog") land here.
  if (!isQuestion && MUSIC_PATTERNS.some(p => p.test(lower))) {
    return { type: 'music', subtype: detectMusicSubtype(lower), confidence: 'high' };
  }

  // 4. Image edit detection (before general image detection)
  if (!isQuestion && context.lastImageUrl) {
    if (ASPECT_RATIO_PATTERNS.some(p => p.test(text))) {
      return { type: 'image_edit', subtype: 'aspect_ratio', confidence: 'high' };
    }
    if (EDIT_PATTERNS.some(p => p.test(text))) {
      return { type: 'image_edit', subtype: 'general', confidence: 'high' };
    }
  }

  // 5. Infographic/flyer detection (before general image)
  if (!isQuestion && INFOGRAPHIC_PATTERNS.some(p => p.test(lower))) {
    return { type: 'image', subtype: 'infographic', confidence: 'high' };
  }

  // 6. General image detection
  if (!isQuestion && IMAGE_PATTERNS.some(p => p.test(lower))) {
    const subtype = detectImageSubtype(lower);
    return { type: 'image', subtype, confidence: 'high' };
  }

  // 7. Fall through to chat
  const chatSubtype = isQuestion ? 'question' : detectChatSubtype(lower);
  return { type: 'chat', subtype: chatSubtype, confidence: isQuestion ? 'high' : 'default' };
}

// ── Subtype helpers ──────────────────────────────────────────────────────────

function detectChatSubtype(lower) {
  for (const [subtype, pattern] of Object.entries(CHAT_SUBTYPES)) {
    if (pattern.test(lower)) return subtype;
  }
  return 'general';
}

function detectImageSubtype(lower) {
  if (/\b(logo|brand|icon|badge|emblem)\b/i.test(lower)) return 'logo';
  if (/\b(diagram|chart|schematic|blueprint|flowchart|wireframe|mindmap)\b/i.test(lower)) return 'diagram';
  if (/\b(photo|photograph|portrait|headshot|realistic)\b/i.test(lower)) return 'photo';
  if (/\b(art|artistic|fantasy|surreal|illustration|painting|anime)\b/i.test(lower)) return 'art';
  return 'general';
}

function detectVideoSubtype(lower) {
  if (/\b(cinematic|film|movie|dramatic|epic|documentary)\b/i.test(lower)) return 'cinematic';
  if (/\b(talking|speaking|dialogue|lip\s*sync|speech|saying)\b/i.test(lower)) return 'talking';
  if (/\b(dance|dancing|choreography|movement|music\s*video)\b/i.test(lower)) return 'dance';
  if (/\b(animate|animation|animated|bring\s+to\s+life)\b/i.test(lower)) return 'animation';
  return 'general';
}

function detectMusicSubtype(lower) {
  if (/\binstrumental\b/i.test(lower)) return 'instrumental';
  if (/\b(jingle|advert|commercial|ad\s+music)\b/i.test(lower)) return 'jingle';
  if (/\blyrics?\b/i.test(lower)) return 'lyrics';
  if (/\b(soundtrack|theme(?:\s+song|\s+tune)?|score|background\s+music|intro|outro)\b/i.test(lower)) return 'soundtrack';
  return 'general';
}
