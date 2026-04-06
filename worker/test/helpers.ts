/**
 * Shared test helpers for integration tests.
 */

interface ApiKeyOptions {
  created_at?: string;
  tier?: number;
  admin?: boolean;
}

interface UploadRequestOptions {
  method?: string;
  headers?: Record<string, string>;
  gzip?: boolean;
}

/**
 * Seed an API key into the API_KEYS KV namespace.
 */
export async function seedApiKey(
  env: { API_KEYS: KVNamespace },
  key: string,
  options: ApiKeyOptions = {}
) {
  const data = {
    created_at: options.created_at ?? new Date(Date.now() - 7200_000).toISOString(), // 2 hours ago by default
    tier: options.tier ?? 3,
    ...(options.admin ? { admin: true } : {}),
  };
  await env.API_KEYS.put(key, JSON.stringify(data));
}

let _jsonlCounter = 0;

/**
 * Generate valid JSONL test payload with the given number of conversations.
 * Each call produces unique content to avoid content-hash dedup collisions.
 */
export function makeJsonl(count: number): string {
  const batch = ++_jsonlCounter;
  const lines: string[] = [];
  for (let i = 0; i < count; i++) {
    lines.push(
      JSON.stringify({
        conversation_id: `test-conv-b${batch}-${i}`,
        turns: [
          { role: "user", content: `Hello, this is test message ${i} batch ${batch}` },
          {
            role: "assistant",
            content: `Hi there! This is response ${i} from the assistant batch ${batch}.`,
          },
        ],
        turn_count: 2,
        language: "en",
        quality_signals: {
          avg_response_len: 40,
          has_code: false,
          vocab_diversity: 0.8,
          total_length: 80,
          user_msg_count: 1,
          assistant_msg_count: 1,
        },
        ner_scrubbed: false,
      })
    );
  }
  return lines.join("\n");
}

/**
 * Create a Request object for POST /upload.
 */
export async function makeUploadRequest(
  body: string,
  apiKey: string,
  options: UploadRequestOptions = {}
): Promise<Request> {
  const headers: Record<string, string> = {
    "X-API-Key": apiKey,
    ...options.headers,
  };

  let requestBody: BodyInit = body;

  if (options.gzip) {
    // Compress the body using CompressionStream
    const stream = new Blob([body]).stream().pipeThrough(
      new CompressionStream("gzip")
    );
    const compressed = await new Response(stream).arrayBuffer();
    requestBody = compressed;
    headers["Content-Encoding"] = "gzip";
    headers["Content-Length"] = String(compressed.byteLength);
  }

  return new Request("http://localhost/upload", {
    method: "POST",
    headers,
    body: requestBody,
  });
}
