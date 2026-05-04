/**
 * SoulPrint Dynamic Intelligence SDK
 * 
 * Single entry point for intent detection and model routing.
 * Drop this folder into any project and import from here.
 * 
 * @example
 * import { route, detectIntent, selectChatModel, selectImageModel, selectVideoModel } from './index.js';
 * 
 * const result = route({ message: 'Create a cinematic video of a sunset' });
 * // => { intent: { type: 'video', subtype: 'cinematic' }, model: { key: 'sora-2', ... }, reason: '...', confidence: 'high' }
 */

export { detectIntent } from './intent-detector.js';
export { selectChatModel, selectImageModel, selectVideoModel } from './model-router.js';
export {
  setModels, resetModels,
  getChatModels, getImageModels, getVideoModels,
  getChatModel, getImageModel, getVideoModel,
  getPremiumChatModels, getStandardChatModels,
  getModelsByStrength,
} from './model-registry.js';

import { detectIntent } from './intent-detector.js';
import { selectChatModel, selectImageModel, selectVideoModel } from './model-router.js';

/**
 * All-in-one routing function.
 * Detects intent from the message, then selects the optimal model.
 * 
 * @param {object} input
 * @param {string} input.message - The user's input text
 * @param {object} [input.context] - Optional context
 * @param {boolean} [input.context.hasImageAttachment] - Message includes an image
 * @param {string|null} [input.context.lastImageUrl] - Last image URL in conversation
 * @param {boolean} [input.context.hasVideoAttachment] - Message includes a video
 * @param {object} [input.options] - Routing options
 * @param {string} [input.options.preferredModel] - User's preferred model key
 * @param {string} [input.options.tier] - 'premium' | 'standard' | 'any'
 * @returns {{
 *   intent: { type: string, subtype: string|null, confidence: string },
 *   model: { key: string, name: string, provider: string, tier: string, strengths: string[] } | null,
 *   reason: string,
 *   confidence: string
 * }}
 */
export function route({ message, context = {}, options = {} }) {
  const intent = detectIntent(message, context);

  let modelResult = null;

  switch (intent.type) {
    case 'chat':
      modelResult = selectChatModel(message, options);
      break;
    case 'image':
    case 'image_edit':
      modelResult = selectImageModel(message, options);
      break;
    case 'video':
      modelResult = selectVideoModel(message, options);
      break;
    case 'refused':
      // No model needed — user is complaining
      return { intent, model: null, reason: '🚫 User refused/complained about AI action', confidence: 'high' };
    default:
      modelResult = selectChatModel(message, options);
  }

  return {
    intent,
    model: modelResult.model,
    reason: modelResult.reason,
    confidence: modelResult.confidence,
  };
}
