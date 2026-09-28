import adapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "fs";
import { dirname, join } from "path";
import preprocess from "svelte-preprocess";
import { fileURLToPath } from "url";

// This prevents errors being shown when opening VSCode on the root of the
// project, instead of the ts folder.
const tsFolder = dirname(fileURLToPath(import.meta.url));

/** @type {import('@sveltejs/kit').Config} */
const config = {
    // preprocess() slows things down by about 10%, but allows us to use :global { ... }
    preprocess: [vitePreprocess(), preprocess()],

    kit: {
        // Builds and svelte-check both generate server options in .svelte-kit.
        // Use the same version so the HTML and client agree on the payload name.
        version: { name: readFileSync(join(tsFolder, "../.version"), "utf8").trim() },
        adapter: adapter(
            { pages: "../out/sveltekit", fallback: "index.html", precompress: false },
        ),
        alias: {
            "@tslib": join(tsFolder, "lib/tslib"),
            "@generated": join(tsFolder, "../out/ts/lib/generated"),
        },
        files: {
            lib: join(tsFolder, "lib"),
            routes: join(tsFolder, "routes"),
        },
        // outside of out/; as things break when out/ is a symlink
        outDir: join(tsFolder, ".svelte-kit"),
        output: { preloadStrategy: "preload-mjs" },
        prerender: {
            crawl: false,
            entries: [],
        },
        paths: {},
        csp: { mode: "hash", directives: { "script-src": ["'self'"] } },
    },
};

export default config;
