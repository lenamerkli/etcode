> ## Documentation Index
> Fetch the complete documentation index at: https://openrouter.ai/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Quickstart

> Get started with OpenRouter

OpenRouter gives you access to hundreds of AI models through a single API endpoint. It handles fallbacks automatically and picks the most cost-effective option for each request.

There are three ways to integrate with OpenRouter, depending on how much control you want:

| Approach | Best for |
| - | - |
| **[API](#using-the-openrouter-api)** | Full control, any language, no dependencies |
| **[Client SDKs](#using-the-client-sdks)** | Type-safe model calls with minimal overhead |
| **[Agent SDK](#using-the-agent-sdk)** | Building agents with tool use, loops, and state |

> [!NOTE]
>
> Read [github.com/OpenRouterTeam/skills/tree/main/skills/create-agent-tui](https://github.com/OpenRouterTeam/skills/tree/main/skills/create-agent-tui) and follow the instructions to build an agent using OpenRouter.

> [!TIP]
>
> Looking for information about free models and rate limits? Please see the [FAQ](https://openrouter.ai/docs/faq.md#how-are-rate-limits-calculated)

In the examples below, the OpenRouter-specific headers are optional. Setting them allows your app to appear on the OpenRouter leaderboards. For detailed information about app attribution, see our [App Attribution guide](https://openrouter.ai/docs/app-attribution.md).

***

## Using the OpenRouter API

The most direct way to use OpenRouter. Send standard HTTP requests to the `/api/v1/chat/completions` endpoint. It works with any language or framework.

> [!TIP]
>
> You can use the interactive [Request Builder](https://openrouter.ai/request-builder) to generate OpenRouter API requests in the language of your choice.

> [!NOTE]
>
> The examples below use `~openai/gpt-sol-latest`, a [latest alias](https://openrouter.ai/docs/guides/routing/routers/latest-resolution.md) that always resolves to the newest model in the OpenAI GPT Sol family, so your code keeps using the freshest version without redeploying. You can substitute any model slug here. Browse the full catalog at [openrouter.ai/models](https://openrouter.ai/models), or list every available slug programmatically via the [`GET /api/v1/models`](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties) endpoint.

```python title="Python" lines theme={null}
import requests
import json

response = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  headers={
    "Authorization": "Bearer <OPENROUTER_API_KEY>",
    "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
    "X-OpenRouter-Title": "<YOUR_SITE_NAME>", # Optional. Site title for rankings on openrouter.ai.
  },
  data=json.dumps({
    "model": "~openai/gpt-sol-latest",
    "messages": [
      {
        "role": "user",
        "content": "What is the meaning of life?"
      }
    ]
  })
)
```

```typescript title="TypeScript (fetch)" lines theme={null}
fetch('https://openrouter.ai/api/v1/chat/completions', {
  method: 'POST',
  headers: {
    Authorization: 'Bearer <OPENROUTER_API_KEY>',
    'HTTP-Referer': '<YOUR_SITE_URL>', // Optional. Site URL for rankings on openrouter.ai.
    'X-OpenRouter-Title': '<YOUR_SITE_NAME>', // Optional. Site title for rankings on openrouter.ai.
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    model: '~openai/gpt-sol-latest',
    messages: [
      {
        role: 'user',
        content: 'What is the meaning of life?',
      },
    ],
  }),
});
```

```shell title="Shell" lines theme={null}
curl https://openrouter.ai/api/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -d '{
  "model": "~openai/gpt-sol-latest",
  "messages": [
    {
      "role": "user",
      "content": "What is the meaning of life?"
    }
  ]
}'
```

The API also supports [streaming](https://openrouter.ai/docs/api_reference/streaming.md). You can also use the [OpenAI SDK](#using-the-openai-sdk) pointed at OpenRouter as a drop-in replacement.

> [!NOTE]
>
> For EU in-region routing on Business or Enterprise, send chat completion requests to `https://eu.openrouter.ai/api/v1/chat/completions`, or set your SDK's base URL to `https://eu.openrouter.ai/api/v1`, with an EU-eligible model. [EU in-region routing on Business](https://openrouter.ai/business/eu) explains what is processed in Europe, what may be stored or processed in the US, and how BYOK requests are handled.

***

## Using the Client SDKs

The [Client SDKs](https://openrouter.ai/docs/client-sdks/overview.md) wrap the OpenRouter API with full type safety, auto-generated types from the OpenAPI spec, and zero boilerplate. It's intentionally lean, a thin layer over the REST API.

First, install the SDK:

```bash title="npm" lines theme={null}
npm install @openrouter/sdk
```

```bash title="pnpm" lines theme={null}
pnpm add @openrouter/sdk
```

```bash title="yarn" lines theme={null}
yarn add @openrouter/sdk
```

```bash title="bun" lines theme={null}
bun add @openrouter/sdk
```

```bash title="deno" lines theme={null}
deno add npm:@openrouter/sdk
```

```bash title="pip" lines theme={null}
pip install openrouter
```

Then use it in your code:

```typescript title="TypeScript" lines theme={null}
import { OpenRouter } from '@openrouter/sdk';

const client = new OpenRouter({
  apiKey: '<OPENROUTER_API_KEY>',
  httpReferer: '<YOUR_SITE_URL>', // Optional. Site URL for rankings on openrouter.ai.
  appTitle: '<YOUR_SITE_NAME>', // Optional. Site title for rankings on openrouter.ai.
});

const completion = await client.chat.send({
  chatRequest: {
    model: '~openai/gpt-sol-latest',
    messages: [
      {
        role: 'user',
        content: 'What is the meaning of life?',
      },
    ],
  },
});

if (completion instanceof ReadableStream) {
  throw new Error('Expected a non-streaming response');
}

console.log(completion.choices[0].message.content);
```

```python title="Python" lines theme={null}
from openrouter import OpenRouter
import os

with OpenRouter(api_key=os.getenv("OPENROUTER_API_KEY")) as client:
    response = client.chat.send(
        model="~openai/gpt-sol-latest",
        messages=[
            {"role": "user", "content": "What is the meaning of life?"}
        ],
    )

    print(response.choices[0].message.content)
```

See the full [Client SDKs documentation](https://openrouter.ai/docs/client-sdks/overview.md) for streaming, embeddings, and the complete API reference.

***

## Using the Agent SDK

The [Agent SDK](https://openrouter.ai/docs/agent-sdk/overview.md) (`@openrouter/agent`) provides higher-level primitives for building AI agents. It handles multi-turn conversation loops, tool execution, and state management automatically via the `callModel` function.

Install the package:

```bash title="npm" lines theme={null}
npm install @openrouter/agent
```

```bash title="pnpm" lines theme={null}
pnpm add @openrouter/agent
```

```bash title="yarn" lines theme={null}
yarn add @openrouter/agent
```

```bash title="bun" lines theme={null}
bun add @openrouter/agent
```

```bash title="deno" lines theme={null}
deno add npm:@openrouter/agent
```

Build an agent with tools:

```typescript expandable lines theme={null}
import { OpenRouter, tool } from '@openrouter/agent';
import { z } from 'zod';

const openrouter = new OpenRouter({
  apiKey: process.env.OPENROUTER_API_KEY,
});

const weatherTool = tool({
  name: 'get_weather',
  description: 'Get the current weather for a location',
  inputSchema: z.object({
    location: z.string().describe('City name'),
  }),
  execute: async ({ location }) => {
    return { temperature: 72, condition: 'sunny', location };
  },
});

const result = openrouter.callModel({
  model: '~anthropic/claude-sonnet-latest',
  input: 'What is the weather in San Francisco?',
  tools: [weatherTool],
});

const text = await result.getText();
console.log(text);
```

The SDK sends the prompt, receives a tool call from the model, executes `get_weather`, feeds the result back, and returns the final response, all in one `callModel` invocation.

See the full [Agent SDK documentation](https://openrouter.ai/docs/agent-sdk/overview.md) for stop conditions, streaming, dynamic parameters, and more.

***

## Using the OpenAI SDK

You can also use the OpenAI SDK pointed at OpenRouter as a drop-in replacement. This is useful if you have existing code built on the OpenAI SDK and want to access OpenRouter's model catalog without changing your code structure.

```typescript title="Typescript" expandable lines theme={null}
import OpenAI from 'openai';

const openai = new OpenAI({
  baseURL: 'https://openrouter.ai/api/v1',
  apiKey: '<OPENROUTER_API_KEY>',
  defaultHeaders: {
    'HTTP-Referer': '<YOUR_SITE_URL>', // Optional. Site URL for rankings on openrouter.ai.
    'X-OpenRouter-Title': '<YOUR_SITE_NAME>', // Optional. Site title for rankings on openrouter.ai.
  },
});

async function main() {
  const completion = await openai.chat.completions.create({
    model: '~openai/gpt-sol-latest',
    messages: [
      {
        role: 'user',
        content: 'What is the meaning of life?',
      },
    ],
  });

  console.log(completion.choices[0].message);
}

main();
```

```python title="Python" expandable lines theme={null}
from openai import OpenAI

client = OpenAI(
  base_url="https://openrouter.ai/api/v1",
  api_key="<OPENROUTER_API_KEY>",
)

completion = client.chat.completions.create(
  extra_headers={
    "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
    "X-OpenRouter-Title": "<YOUR_SITE_NAME>", # Optional. Site title for rankings on openrouter.ai.
  },
  model="~openai/gpt-sol-latest",
  messages=[
    {
      "role": "user",
      "content": "What is the meaning of life?"
    }
  ]
)

print(completion.choices[0].message.content)
```

## Using third-party SDKs

For information about using third-party SDKs and frameworks with OpenRouter, please [see our frameworks documentation.](https://openrouter.ai/docs/guides/community/frameworks-and-integrations-overview.md)

***

## Building with an AI assistant

If you write code with an AI coding tool (Claude Code, Cursor, Codex, and others), connect the [OpenRouter MCP server](https://openrouter.ai/docs/guides/overview/mcp-server.md). It's a remote server hosted by OpenRouter, so there's nothing to install. Your assistant can pull live OpenRouter data (which models exist, what they cost, your credit balance, usage rankings) and search these docs while you build. That way its suggestions reflect current data instead of stale training knowledge. Add one URL to your MCP client and approve an OAuth login:

```bash theme={null}
https://mcp.openrouter.ai/mcp
```

See the [MCP server guide](https://openrouter.ai/docs/guides/overview/mcp-server.md) for per-client setup and the full tool list. To run models in your app, keep calling the OpenRouter API directly.

> ## Documentation Index
> Fetch the complete documentation index at: https://openrouter.ai/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Image Inputs

> How to send images to OpenRouter models

Requests with images, to multimodel models, are available via the `/api/v1/chat/completions` API with a multi-part `messages` parameter. The `image_url` can either be a URL or a base64-encoded image. Note that multiple images can be sent in separate content array entries. The number of images you can send in a single request varies per provider and per model. Due to how the content is parsed, we recommend sending the text prompt first, then the images. If the images must come first, we recommend putting it in the system prompt.

OpenRouter supports both **direct URLs** and **base64-encoded data** for images:

* **URLs**: More efficient for publicly accessible images as they don't require local encoding
* **Base64**: Required for local files or private images that aren't publicly accessible

### Using Image URLs

Here's how to send an image using a URL:

### Using Base64 Encoded Images

For locally stored images, you can send them using base64 encoding. Here's how to do it:

Supported image content types are:

* `image/png`
* `image/jpeg`
* `image/webp`
* `image/gif`

> ## Documentation Index
> Fetch the complete documentation index at: https://openrouter.ai/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Client Tools

> Use client tools in your prompts

Tool calls (also known as function calls) give an LLM access to external tools. The LLM does not call the tools directly. Instead, it suggests the tool to call. The user then calls the tool separately and provides the results back to the LLM. Finally, the LLM formats the response into an answer to the user's original question.

OpenRouter standardizes the tool calling interface across models and providers, making it easy to integrate external tools with any supported model.

**Supported Models**: You can find models that support tool calling by filtering on [openrouter.ai/models?supported\_parameters=tools](https://openrouter.ai/models?supported_parameters=tools).

If you prefer to learn from a full end-to-end example, keep reading.

## Request Body Examples

Tool calling with OpenRouter involves three key steps. Here are the essential request body formats for each step:

### Step 1: Inference Request with Tools

```json expandable lines theme={null}
{
  "model": "google/gemini-3-flash-preview",
  "messages": [
    {
      "role": "user",
      "content": "What are the titles of some James Joyce books?"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_gutenberg_books",
        "description": "Search for books in the Project Gutenberg library",
        "parameters": {
          "type": "object",
          "properties": {
            "search_terms": {
              "type": "array",
              "items": {"type": "string"},
              "description": "List of search terms to find books"
            }
          },
          "required": ["search_terms"]
        }
      }
    }
  ]
}
```

### Step 2: Tool Execution (Client-Side)

After receiving the model's response with `tool_calls`, execute the requested tool locally and prepare the result:

```javascript lines theme={null}
// Model responds with tool_calls, you execute the tool locally
const toolResult = await searchGutenbergBooks(["James", "Joyce"]);
```

### Step 3: Inference Request with Tool Results

```json expandable lines theme={null}
{
  "model": "google/gemini-3-flash-preview",
  "messages": [
    {
      "role": "user",
      "content": "What are the titles of some James Joyce books?"
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": {
            "name": "search_gutenberg_books",
            "arguments": "{\"search_terms\": [\"James\", \"Joyce\"]}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123",
      "content": "[{\"id\": 4300, \"title\": \"Ulysses\", \"authors\": [{\"name\": \"Joyce, James\"}]}]"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_gutenberg_books",
        "description": "Search for books in the Project Gutenberg library",
        "parameters": {
          "type": "object",
          "properties": {
            "search_terms": {
              "type": "array",
              "items": {"type": "string"},
              "description": "List of search terms to find books"
            }
          },
          "required": ["search_terms"]
        }
      }
    }
  ]
}
```

**Note**: The `tools` parameter must be included in every request (Steps 1 and 3) so the router can validate the tool schema on each call.

### Tool Calling Example

Here is an end-to-end example that gives LLMs the ability to call an external API -- in this case Project Gutenberg, to search for books.

First, let's do some basic setup:

### Define the Tool

Next, we define the tool that we want to call. Remember, the tool is going to get *requested* by the LLM, but the code we are writing here is ultimately responsible for executing the call and returning the results to the LLM.

Note that the "tool" is just a normal function. We then write a JSON "spec" compatible with the OpenAI function calling parameter. We'll pass that spec to the LLM so that it knows this tool is available and how to use it. It will request the tool when needed, along with any arguments. We'll then marshal the tool call locally, make the function call, and return the results to the LLM.

### Tool use and tool results

Let's make the first OpenRouter API call to the model:

The LLM responds with a finish reason of `tool_calls`, and a `tool_calls` array. In a generic LLM response-handler, you would want to check the `finish_reason` before processing tool calls, but here we will assume it's the case. Let's keep going, by processing the tool call:

The messages array now has:

1. Our original request
2. The LLM's response (containing a tool call request)
3. The result of the tool call (a json object returned from the Project Gutenberg API)

Now, we can make a second OpenRouter API call, and hopefully get our result!

The output will be something like:

```text lines theme={null}
Here are some books by James Joyce:

*   *Ulysses*
*   *Dubliners*
*   *A Portrait of the Artist as a Young Man*
*   *Chamber Music*
*   *Exiles: A Play in Three Acts*
```

We did it! We've successfully used a tool in a prompt.

## Interleaved Thinking

Interleaved thinking allows models to reason between tool calls, enabling more sophisticated decision-making after receiving tool results. This feature helps models chain multiple tool calls with reasoning steps in between and make nuanced decisions based on intermediate results.

**Important**: Interleaved thinking increases token usage and response latency. Consider your budget and performance requirements when enabling this feature.

### How Interleaved Thinking Works

With interleaved thinking, the model can:

* Reason about the results of a tool call before deciding what to do next
* Chain multiple tool calls with reasoning steps in between
* Make more nuanced decisions based on intermediate results
* Provide transparent reasoning for its tool selection process

### Example: Multi-Step Research with Reasoning

Here's an example showing how a model might use interleaved thinking to research a topic across multiple sources:

**Initial Request:**

```json expandable lines theme={null}
{
  "model": "anthropic/claude-sonnet-4.5",
  "messages": [
    {
      "role": "user",
      "content": "Research the environmental impact of electric vehicles and provide a comprehensive analysis."
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_academic_papers",
        "description": "Search for academic papers on a given topic",
        "parameters": {
          "type": "object",
          "properties": {
            "query": {"type": "string"},
            "field": {"type": "string"}
          },
          "required": ["query"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "get_latest_statistics",
        "description": "Get latest statistics on a topic",
        "parameters": {
          "type": "object",
          "properties": {
            "topic": {"type": "string"},
            "year": {"type": "integer"}
          },
          "required": ["topic"]
        }
      }
    }
  ]
}
```

**Model's Reasoning and Tool Calls:**

1. **Initial Thinking**: "I need to research electric vehicle environmental impact. Let me start with academic papers to get peer-reviewed research."

2. **First Tool Call**: `search_academic_papers({"query": "electric vehicle lifecycle environmental impact", "field": "environmental science"})`

3. **After First Tool Result**: "The papers show mixed results on manufacturing impact. I need current statistics to complement this academic research."

4. **Second Tool Call**: `get_latest_statistics({"topic": "electric vehicle carbon footprint", "year": 2024})`

5. **After Second Tool Result**: "Now I have both academic research and current data. Let me search for manufacturing-specific studies to address the gaps I found."

6. **Third Tool Call**: `search_academic_papers({"query": "electric vehicle battery manufacturing environmental cost", "field": "materials science"})`

7. **Final Analysis**: Synthesizes all gathered information into a comprehensive response.

### Best Practices for Interleaved Thinking

* **Clear Tool Descriptions**: Provide detailed descriptions so the model can reason about when to use each tool
* **Structured Parameters**: Use well-defined parameter schemas to help the model make precise tool calls
* **Context Preservation**: Maintain conversation context across multiple tool interactions
* **Error Handling**: Design tools to provide meaningful error messages that help the model adjust its approach

### Implementation Considerations

When implementing interleaved thinking:

* Models may take longer to respond due to additional reasoning steps
* Token usage will be higher due to the reasoning process
* The quality of reasoning depends on the model's capabilities
* Some models may be better suited for this approach than others

## A Simple Agentic Loop

In the example above, the calls are made explicitly and sequentially. To handle a wide variety of user inputs and tool calls, you can use an agentic loop.

Here's an example of a simple agentic loop (using the same `tools` and initial `messages` as above):

## Best Practices and Advanced Patterns

### Function Definition Guidelines

When defining tools for LLMs, follow these best practices:

**Clear and Descriptive Names**: Use descriptive function names that clearly indicate the tool's purpose.

```json lines theme={null}
// Good: Clear and specific
{ "name": "get_weather_forecast" }
```

```json lines theme={null}
// Avoid: Too vague
{ "name": "weather" }
```

**Comprehensive Descriptions**: Provide detailed descriptions that help the model understand when and how to use the tool.

```json lines theme={null}
{
  "description": "Get current weather conditions and 5-day forecast for a specific location. Supports cities, zip codes, and coordinates.",
  "parameters": {
    "type": "object",
    "properties": {
      "location": {
        "type": "string",
        "description": "City name, zip code, or coordinates (lat,lng). Examples: 'New York', '10001', '40.7128,-74.0060'"
      },
      "units": {
        "type": "string",
        "enum": ["celsius", "fahrenheit"],
        "description": "Temperature unit preference",
        "default": "celsius"
      }
    },
    "required": ["location"]
  }
}
```

### Streaming with Tool Calls

When using streaming responses with tool calls, handle the different content types appropriately:

```typescript expandable lines theme={null}
const stream = await fetch('/api/chat/completions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    model: 'anthropic/claude-sonnet-4.5',
    messages: messages,
    tools: tools,
    stream: true
  })
});

const reader = stream.body.getReader();
let toolCalls = [];

while (true) {
  const { done, value } = await reader.read();
  if (done) {
    break;
  }

  const chunk = new TextDecoder().decode(value);
  const lines = chunk.split('\n').filter(line => line.trim());

  for (const line of lines) {
    if (line.startsWith('data: ')) {
      const data = JSON.parse(line.slice(6));

      if (data.choices[0].delta.tool_calls) {
        toolCalls.push(...data.choices[0].delta.tool_calls);
      }

      if (data.choices[0].delta.finish_reason === 'tool_calls') {
        await handleToolCalls(toolCalls);
      } else if (data.choices[0].delta.finish_reason === 'stop') {
        // Regular completion without tool calls
        break;
      }
    }
  }
}
```

### Tool Choice Configuration

Control tool usage with the `tool_choice` parameter:

```json lines theme={null}
// Let model decide (default)
{ "tool_choice": "auto" }
```

```json lines theme={null}
// Disable tool usage
{ "tool_choice": "none" }
```

```json lines theme={null}
// Force specific tool
{
  "tool_choice": {
    "type": "function",
    "function": {"name": "search_database"}
  }
}
```

### Parallel Tool Calls

Control whether multiple tools can be called simultaneously with the `parallel_tool_calls` parameter (default is true for most models):

```json lines theme={null}
// Disable parallel tool calls - tools will be called sequentially
{ "parallel_tool_calls": false }
```

When `parallel_tool_calls` is `false`, the model will only request one tool call at a time instead of potentially multiple calls in parallel.

### Multi-Tool Workflows

Design tools that work well together:

```json expandable lines theme={null}
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_products",
        "description": "Search for products in the catalog"
      }
    },
    {
      "type": "function",
      "function": {
        "name": "get_product_details",
        "description": "Get detailed information about a specific product"
      }
    },
    {
      "type": "function",
      "function": {
        "name": "check_inventory",
        "description": "Check current inventory levels for a product"
      }
    }
  ]
}
```

This allows the model to naturally chain operations: search → get details → check inventory.

### Reliability Tracking

OpenRouter tracks how reliably each provider completes tool calls and surfaces this as the **Tool Call Error Rate** on the Performance tab of every model page. The same signal drives [Auto Exacto](https://openrouter.ai/docs/guides/routing/auto-exacto.md) provider ordering on tool-calling requests. For the exact validator, JSON Schema draft, regex semantics, and per-tool-call classification, see [How Tool-Calling Success Rate Is Measured](https://openrouter.ai/docs/guides/routing/auto-exacto.md#how-tool-calling-success-rate-is-measured).

For more details on OpenRouter's message format and tool parameters, see the [API Reference](https://openrouter.ai/docs/api_reference/overview.md).


