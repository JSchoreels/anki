// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface PostProtoOptions {
    /** True by default. Shows a dialog with the error message, then rethrows. */
    alertOnError?: boolean;
}

export interface PostProtoResponse<T> {
    output: T;
    headers: Headers;
}

function graphDebugLoggingEnabled(): boolean {
    return typeof location !== "undefined"
        && new URLSearchParams(location.search).has("graphDebug");
}

function formatGraphDetails(details: Record<string, unknown>): string {
    return JSON.stringify(details);
}

function logGraphPostProto(message: string, details: Record<string, unknown>): void {
    const text = `${message}: ${formatGraphDetails(details)}`;
    if (graphDebugLoggingEnabled()) {
        console.warn(text);
    } else {
        console.debug(text);
    }
}

export async function postProto<T>(
    method: string,
    input: { toBinary(): Uint8Array; getType(): { typeName: string } },
    outputType: { fromBinary(arr: Uint8Array): T },
    options: PostProtoOptions = {},
): Promise<T> {
    const { output } = await postProtoWithResponse(method, input, outputType, options);
    return output;
}

export async function postProtoWithResponse<T>(
    method: string,
    input: { toBinary(): Uint8Array; getType(): { typeName: string } },
    outputType: { fromBinary(arr: Uint8Array): T },
    options: PostProtoOptions = {},
): Promise<PostProtoResponse<T>> {
    try {
        const start = performance.now();
        const inputBytes = input.toBinary();
        const path = `/_anki/${method}`;
        if (method === "graphs") {
            logGraphPostProto("graphs postProto request body encoded", {
                inputBytes: inputBytes.length,
                elapsedMs: performance.now() - start,
            });
        }
        const response = await postProtoInner(path, inputBytes);
        const outputBytes = response.body;
        const fetchElapsedMs = performance.now() - start;
        if (method === "graphs") {
            logGraphPostProto("graphs postProto body received", {
                outputBytes: outputBytes.length,
                fetchElapsedMs,
            });
        }
        const decodeStart = performance.now();
        if (method === "graphs") {
            logGraphPostProto("graphs postProto decode started", {
                outputBytes: outputBytes.length,
                elapsedMs: performance.now() - start,
            });
        }
        const output = outputType.fromBinary(outputBytes);
        const decodeElapsedMs = performance.now() - decodeStart;
        if (method === "graphs") {
            logGraphPostProto("graphs postProto decoded", {
                inputBytes: inputBytes.length,
                outputBytes: outputBytes.length,
                fetchElapsedMs,
                decodeElapsedMs,
                elapsedMs: performance.now() - start,
            });
        }
        return { output, headers: response.headers };
    } catch (err) {
        const { alertOnError = true } = options;
        if (alertOnError && !(err instanceof Error && err.message === "500: Interrupted")) {
            alert(err);
        }
        throw err;
    }
}

interface PostProtoInnerResponse {
    body: Uint8Array;
    headers: Headers;
}

async function postProtoInner(
    url: string,
    body: Uint8Array,
): Promise<PostProtoInnerResponse> {
    const start = performance.now();
    const graphRequest = url === "/_anki/graphs";
    if (graphRequest) {
        logGraphPostProto("graphs fetch started", {
            bodyBytes: body.length,
        });
    }
    const result = await fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/binary",
        },
        body,
    });
    if (graphRequest) {
        logGraphPostProto("graphs fetch response headers received", {
            status: result.status,
            ok: result.ok,
            contentLength: result.headers.get("content-length"),
            elapsedMs: performance.now() - start,
        });
    }
    if (!result.ok) {
        let msg = "something went wrong";
        try {
            msg = await result.text();
        } catch {
            // ignore
        }
        throw new Error(`${result.status}: ${msg}`);
    }
    if (graphRequest) {
        logGraphPostProto("graphs fetch body read started", {
            elapsedMs: performance.now() - start,
        });
    }
    const respBuf = await result.arrayBuffer();
    if (graphRequest) {
        logGraphPostProto("graphs fetch body read finished", {
            bytes: respBuf.byteLength,
            elapsedMs: performance.now() - start,
        });
    }
    return { body: new Uint8Array(respBuf), headers: result.headers };
}
