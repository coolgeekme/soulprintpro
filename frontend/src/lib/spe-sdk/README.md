# SoulPrint Dynamic Intelligence SDK

Standalone, framework-agnostic routing engine for multi-model AI applications.
Drop these files into any JavaScript/TypeScript project.

## Files

| File | Purpose |
|------|----------|
| `intent-detector.js` | Analyzes user input and detects intent (chat, image, video, edit, infographic) |
| `model-router.js` | Selects the optimal AI model based on intent, content analysis, and context |
| `model-registry.js` | Registry of all available chat, image, and video models with capabilities |
| `index.js` | Main entry point — single `route()` call handles everything |
| `types.d.ts` | TypeScript definitions |

## Quick Start

```javascript
import { route } from './index.js';

const result = route({
  message: 'Create a cinematic video of a sunset over the ocean',
  context: { hasImageAttachment: false, lastImageUrl: null }
});

console.log(result);
// {
//   intent: 'video',
//   model: { key: 'sora-2', name: 'Sora 2', provider: 'openai' },
//   reason: '🎬 Cinematic quality - Sora 2 by OpenAI for high-end video',
//   confidence: 'high'
// }
```

## Intent Detection

```javascript
import { detectIntent } from './intent-detector.js';

detectIntent('Why is the sky blue?');           // { type: 'chat', subtype: 'question' }
detectIntent('Generate a photo of a cat');       // { type: 'image' }
detectIntent('Create a video of waves');         // { type: 'video' }
detectIntent('Make this image square');          // { type: 'image_edit', subtype: 'aspect_ratio' }
detectIntent('Design me an infographic about SEO'); // { type: 'image', subtype: 'infographic' }
```

## Model Selection

```javascript
import { selectChatModel, selectImageModel, selectVideoModel } from './model-router.js';

selectChatModel('Explain quantum physics');       // GPT-5.2 (reasoning)
selectChatModel('Write me a love poem');          // Claude Opus (writing)
selectChatModel('What is the weather today?');    // Sonar Pro (web search)
selectImageModel('A photorealistic sunset');      // Nano Banana
selectImageModel('Design a logo for my brand');   // Seedream (text rendering)
selectVideoModel('Cinematic drone shot');          // Sora 2
selectVideoModel('Person talking to camera');      // Wan 2.6 (lip sync)
```

## Custom Model Registry

Override the default models with your own:

```javascript
import { route } from './index.js';
import { setModels } from './model-registry.js';

setModels({
  chat: [
    { key: 'my-model', name: 'My Custom Model', provider: 'custom', tier: 'premium',
      strengths: ['reasoning', 'analysis'] }
  ],
  image: [...],
  video: [...]
});
```

## Zero Dependencies

No npm packages required. Pure JavaScript with optional TypeScript definitions.
Works in Node.js, Deno, Bun, browsers, or any JS runtime.
