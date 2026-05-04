/**
 * Model Router — Selects the optimal AI model based on intent and content analysis.
 * 
 * Uses keyword matching, pattern analysis, and confidence scoring to route
 * requests to the best-fit model from the registry.
 * 
 * Zero dependencies. Pure JavaScript.
 */

import { getChatModels, getImageModels, getVideoModels } from './model-registry.js';

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT MODEL SELECTION
// ═══════════════════════════════════════════════════════════════════════════════

const CHAT_ROUTING_RULES = [
  {
    name: 'deep-reasoning',
    test: /\b(prove|theorem|proof|logic|derive|axiom|formal|mathematical\s+proof|step\s+by\s+step.*reason)\b/i,
    strength: 'deep-reasoning',
    fallback: 'reasoning',
    reason: '🧠 Deep reasoning — complex logical/mathematical problem',
  },
  {
    name: 'coding',
    test: /\b(code|function|class|api|debug|error|bug|compile|syntax|regex|implement|refactor|typescript|javascript|python|react|sql|git|deploy|dockerfile|kubernetes|aws|backend|frontend|fullstack|algorithm|data\s+structure)\b/i,
    strength: 'coding',
    reason: '💻 Code task — routed to coding-optimized model',
  },
  {
    name: 'creative-writing',
    test: /\b(write|poem|story|song|lyrics|script|essay|letter|novel|fiction|creative|compose|draft|narrative|dialogue|chapter|prose|verse|haiku|sonnet)\b/i,
    strength: 'writing',
    fallback: 'creative',
    reason: '✍️ Creative writing — routed to nuance-optimized model',
  },
  {
    name: 'web-search',
    test: /\b(search|look\s+up|find\s+(?:the\s+)?latest|current|today|right\s+now|news|weather|score|price|stock|trending|recent|2025|2026|live|real-?time)\b/i,
    strength: 'web-search',
    fallback: 'real-time',
    reason: '🔍 Real-time info needed — routed to web-search model',
  },
  {
    name: 'analysis',
    test: /\b(analyze|analysis|evaluate|compare|assess|review|critique|break\s*down|pros\s+and\s+cons|summarize|benchmark|audit|inspect)\b/i,
    strength: 'analysis',
    reason: '📊 Analysis task — routed to analysis-optimized model',
  },
  {
    name: 'math',
    test: /\b(calculate|compute|solve|equation|formula|derivative|integral|probability|statistics|algebra|geometry|calculus|matrix|linear\s+algebra)\b/i,
    strength: 'math',
    fallback: 'reasoning',
    reason: '🔢 Math/computation — routed to math-optimized model',
  },
  {
    name: 'multimodal',
    test: /\b(describe\s+this\s+image|what\s+is\s+in\s+this|analyze\s+this\s+(?:image|photo|picture|screenshot)|read\s+this|ocr|extract\s+text)\b/i,
    strength: 'vision',
    fallback: 'multimodal',
    reason: '👁️ Vision/multimodal — routed to vision-capable model',
  },
];

/**
 * Select the best chat model for a given message.
 * 
 * @param {string} message - The user's input text
 * @param {object} [options]
 * @param {string} [options.preferredModel] - User's preferred model key (overrides routing)
 * @param {string} [options.tier] - 'premium' | 'standard' | 'any' (default: 'any')
 * @returns {{ model: object, reason: string, confidence: string }}
 */
export function selectChatModel(message, options = {}) {
  const models = getChatModels();
  
  // User explicitly selected a model
  if (options.preferredModel) {
    const preferred = models.find(m => m.key === options.preferredModel);
    if (preferred) {
      return { model: preferred, reason: `🎯 User selected ${preferred.name}`, confidence: 'user-selected' };
    }
  }

  const tier = options.tier || 'any';
  const eligible = tier === 'any' ? models : models.filter(m => m.tier === tier);
  if (eligible.length === 0) return { model: models[0], reason: 'No eligible models', confidence: 'fallback' };

  const lower = (message || '').toLowerCase();

  // Check routing rules in priority order
  for (const rule of CHAT_ROUTING_RULES) {
    if (rule.test.test(lower)) {
      // Find a model with the matching strength
      let match = eligible.find(m => m.strengths.includes(rule.strength));
      if (!match && rule.fallback) {
        match = eligible.find(m => m.strengths.includes(rule.fallback));
      }
      if (match) {
        return { model: match, reason: rule.reason, confidence: 'high' };
      }
    }
  }

  // Default: pick the first premium model (usually the flagship)
  const defaultModel = eligible.find(m => m.tier === 'premium') || eligible[0];
  return { model: defaultModel, reason: '🤖 General query — routed to flagship model', confidence: 'default' };
}

// ═══════════════════════════════════════════════════════════════════════════════
// IMAGE MODEL SELECTION
// ═══════════════════════════════════════════════════════════════════════════════

const IMAGE_ROUTING_RULES = [
  {
    name: 'text-heavy',
    test: /\b(logo|text|typography|sign|poster|banner|flyer|advertisement|brand|label|t-shirt\s+design|title|headline|letters|words|writing)\b/i,
    strength: 'text-rendering',
    reason: '📝 Text rendering — model excels at typography clarity',
  },
  {
    name: 'artistic',
    test: /\b(art|artistic|fantasy|surreal|dream|magical|ethereal|abstract|painting|illustration|anime|manga|concept\s+art|cinematic|epic|dramatic|vibrant)\b/i,
    strength: 'artistic',
    fallback: 'creative',
    reason: '🎨 Artistic/creative style — high-quality art generation',
  },
  {
    name: 'photorealistic',
    test: /\b(photo|photograph|realistic|real|hd|4k|portrait|headshot|professional|corporate|stock\s+photo|documentary)\b/i,
    strength: 'photorealistic',
    reason: '📷 Photorealistic — optimized for natural imagery',
  },
  {
    name: 'product',
    test: /\b(product|ecommerce|catalog|mockup|packaging|commercial|marketing|studio\s+shot|white\s+background)\b/i,
    strength: 'product',
    fallback: 'commercial',
    reason: '🛍️ Product/commercial — clean commercial shots',
  },
  {
    name: 'simple',
    test: /\b(simple|basic|quick|icon|emoji|avatar|thumbnail|sketch|draft)\b/i,
    strength: 'fast',
    reason: '⚡ Quick generation — fast and efficient',
  },
];

/**
 * Select the best image model for a given prompt.
 * 
 * @param {string} prompt - The image generation prompt
 * @param {object} [options]
 * @param {string} [options.preferredModel] - User's preferred model key
 * @returns {{ model: object, reason: string, confidence: string }}
 */
export function selectImageModel(prompt, options = {}) {
  const models = getImageModels();

  if (options.preferredModel && options.preferredModel !== 'smart') {
    const preferred = models.find(m => m.key === options.preferredModel);
    if (preferred) {
      return { model: preferred, reason: `🎯 User selected ${preferred.name}`, confidence: 'user-selected' };
    }
  }

  // Check for explicit model name in prompt
  const explicitModel = parseExplicitModel(prompt, models);
  if (explicitModel) {
    return { model: explicitModel, reason: `🎯 Explicitly requested ${explicitModel.name}`, confidence: 'high' };
  }

  const lower = (prompt || '').toLowerCase();

  for (const rule of IMAGE_ROUTING_RULES) {
    if (rule.test.test(lower)) {
      let match = models.find(m => m.strengths.includes(rule.strength));
      if (!match && rule.fallback) {
        match = models.find(m => m.strengths.includes(rule.fallback));
      }
      if (match) {
        return { model: match, reason: rule.reason, confidence: 'high' };
      }
    }
  }

  // Default
  const defaultModel = models[0];
  return { model: defaultModel, reason: `🖼️ General image — ${defaultModel.name} for versatile quality`, confidence: 'default' };
}

// ═══════════════════════════════════════════════════════════════════════════════
// VIDEO MODEL SELECTION
// ═══════════════════════════════════════════════════════════════════════════════

const VIDEO_ROUTING_RULES = [
  {
    name: 'talking',
    test: /\b(talking|speaking|dialogue|conversation|interview|presentation|lip\s*sync|speech|voice|saying|narrator)\b/i,
    strength: 'lip-sync',
    fallback: 'talking',
    reason: '🎙️ Speech/dialogue — excellent lip sync capabilities',
  },
  {
    name: 'dance',
    test: /\b(dance|dancing|choreography|movement|motion|action|dynamic|energetic|music\s+video)\b/i,
    strength: 'dance',
    fallback: 'motion',
    reason: '💃 Motion/dance — specialized in movement and choreography',
  },
  {
    name: 'cinematic',
    test: /\b(cinematic|film|movie|story|narrative|dramatic|epic|professional|high\s+quality|4k|hd|hollywood|blockbuster)\b/i,
    strength: 'cinematic',
    reason: '🎬 Cinematic quality — high-end video production',
  },
  {
    name: 'realistic',
    test: /\b(realistic|real|photorealistic|natural|documentary|news|authentic)\b/i,
    strength: 'realistic',
    fallback: 'cinematic',
    reason: '📽️ Realistic video — optimized for natural scenes',
  },
  {
    name: 'simple',
    test: /\b(simple|quick|short|basic|demo|test|preview|draft|fast)\b/i,
    strength: 'fast',
    fallback: 'affordable',
    reason: '⚡ Quick generation — fast and affordable',
  },
];

/**
 * Select the best video model for a given prompt.
 * 
 * @param {string} prompt - The video generation prompt
 * @param {object} [options]
 * @param {string} [options.preferredModel] - User's preferred model key
 * @returns {{ model: object, reason: string, confidence: string }}
 */
export function selectVideoModel(prompt, options = {}) {
  const models = getVideoModels();

  if (options.preferredModel && options.preferredModel !== 'smart') {
    const preferred = models.find(m => m.key === options.preferredModel);
    if (preferred) {
      return { model: preferred, reason: `🎯 User selected ${preferred.name}`, confidence: 'user-selected' };
    }
  }

  const lower = (prompt || '').toLowerCase();

  for (const rule of VIDEO_ROUTING_RULES) {
    if (rule.test.test(lower)) {
      let match = models.find(m => m.strengths.includes(rule.strength));
      if (!match && rule.fallback) {
        match = models.find(m => m.strengths.includes(rule.fallback));
      }
      if (match) {
        return { model: match, reason: rule.reason, confidence: 'high' };
      }
    }
  }

  // Default
  const defaultModel = models[0];
  return { model: defaultModel, reason: `🎥 General video — ${defaultModel.name} for versatile quality`, confidence: 'default' };
}

// ═══════════════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

function parseExplicitModel(prompt, models) {
  if (!prompt) return null;
  const lower = prompt.toLowerCase();
  // Check if the user explicitly mentions a model name
  for (const model of models) {
    const nameVariations = [
      model.key.toLowerCase(),
      model.name.toLowerCase(),
      model.name.toLowerCase().replace(/[\s.-]+/g, ''),
    ];
    for (const name of nameVariations) {
      if (lower.includes(name) && name.length > 3) {
        return model;
      }
    }
  }
  return null;
}
