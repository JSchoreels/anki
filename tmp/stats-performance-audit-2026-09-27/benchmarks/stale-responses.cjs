const fs = require("node:fs");
const { spawnSync } = require("node:child_process");
const repo = "/Users/jschoreels/workspace/Anki-Search-Stats-Extended";
const esbuild = require(repo + "/node_modules/esbuild");
const input = "/private/tmp/anki-stats-audit-eqv1ucpr";
const baselineBlock = `export let card_data: Readable<CardData[] | null> = derived([cids], ([$cids], set) => {
    set(null)
    if ($cids) {
        catchErrors(() => getCardData($cids ?? [])).then(set)
    }
})`;
const candidateBlock = `export let card_data: Readable<CardData[] | null> = derived([cids], ([$cids], set) => {
    let active = true
    set(null)
    if ($cids) {
        catchErrors(() => getCardData($cids ?? [])).then(value => { if (active) set(value) })
    }
    return () => { active = false }
})`;
const source = fs.readFileSync(repo + "/src/ts/stores.ts", "utf8");
if (!source.includes(baselineBlock)) { throw new Error("Source changed; review prototype"); }
const rows = [];
(async () => {
    for (const variant of ["baseline", "generation_guard"]) {
        const outfile = input + "/race-" + variant + ".cjs";
        await esbuild.build({
            stdin: {
                contents: `export {card_data,cids} from '${repo}/src/ts/stores.ts';`,
                resolveDir: repo,
                loader: "ts",
            },
            bundle: true,
            platform: "node",
            format: "cjs",
            outfile,
            logLevel: "warning",
            plugins: variant === "baseline" ? [] : [{
                name: "isolated-store-prototype",
                setup(build) {
                    build.onLoad(
                        { filter: /\/stores\.ts$/ },
                        () => ({
                            contents: source.replace(baselineBlock, candidateBlock),
                            loader: "ts",
                            resolveDir: repo + "/src/ts",
                        }),
                    );
                },
            }],
        });
        const probe = `
      global.SSEconfig={}; global.SSEother={lang:'en',lang_ftl:'',fallback_ftl:''};global.alert=()=>{};
      const pending=new Map();global.fetch=(url,options)=>new Promise(resolve=>pending.set(JSON.parse(options.body)[0],resolve));
      const {card_data,cids}=require(${JSON.stringify(outfile)});
      let visible=null,applied=0;card_data.subscribe(x=>{visible=x?.[0]?.id??null;if(x)applied++});
      const response=id=>new Response(JSON.stringify({columns:['id'],data:[[id]]}));
      const next=()=>new Promise(r=>setImmediate(r));
      (async()=>{
        let failures=0;
        for(let i=0;i<100;i++){
          const a=2*i+1,b=a+1;cids.set([a]);cids.set([b]);
          pending.get(b)(response(b));await next();
          pending.get(a)(response(a));await next();
          if(visible!==b)failures++;
        }
        console.log(JSON.stringify({variant:${
            JSON.stringify(variant)
        },races:100,staleFinalResults:failures,appliedResults:applied}));
      })();
    `;
        const result = spawnSync(process.execPath, ["-e", probe], { encoding: "utf8" });
        if (result.status !== 0) { throw new Error(result.stderr); }
        const row = JSON.parse(result.stdout.trim());
        rows.push(row);
        console.log(JSON.stringify(row));
    }
    if (rows[0].staleFinalResults !== 100 || rows[1].staleFinalResults !== 0) {
        throw new Error("Unexpected race behavior");
    }
    fs.writeFileSync(__dirname + "/stale-results.json", JSON.stringify(rows, null, 2));
})().catch(e => {
    console.error(e);
    process.exitCode = 1;
});
