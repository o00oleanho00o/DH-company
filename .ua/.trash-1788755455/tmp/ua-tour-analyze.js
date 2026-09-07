const fs = require('fs');
const path = require('path');

function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!inputPath || !outputPath) {
    console.error('Usage: node ua-tour-analyze.js <input.json> <output.json>');
    process.exit(1);
  }

  let data;
  try {
    data = JSON.parse(fs.readFileSync(inputPath, 'utf-8'));
  } catch (e) {
    console.error('Failed to read/parse input: ' + e.message);
    process.exit(1);
  }

  try {
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const layers = data.layers || [];

    const nodeById = new Map();
    for (const n of nodes) nodeById.set(n.id, n);

    // Only consider edges where BOTH endpoints are in the node set (nodes file
    // is file-level only; edges may reference function/class nodes not present).
    const validEdges = edges.filter(e => nodeById.has(e.source) && nodeById.has(e.target) && e.source !== e.target);

    const fanIn = new Map();
    const fanOut = new Map();
    for (const n of nodes) { fanIn.set(n.id, 0); fanOut.set(n.id, 0); }
    for (const e of validEdges) {
      fanOut.set(e.source, (fanOut.get(e.source) || 0) + 1);
      fanIn.set(e.target, (fanIn.get(e.target) || 0) + 1);
    }

    const fanInRanking = [...fanIn.entries()]
      .map(([id, v]) => ({ id, fanIn: v, name: nodeById.get(id).name }))
      .sort((a, b) => b.fanIn - a.fanIn)
      .slice(0, 20);

    const fanOutRanking = [...fanOut.entries()]
      .map(([id, v]) => ({ id, fanOut: v, name: nodeById.get(id).name }))
      .sort((a, b) => b.fanOut - a.fanOut)
      .slice(0, 20);

    // Percentile thresholds for entry point scoring
    const fanOutValues = [...fanOut.values()].sort((a, b) => a - b);
    const fanInValues = [...fanIn.values()].sort((a, b) => a - b);
    const pct = (arr, p) => arr[Math.min(arr.length - 1, Math.floor(arr.length * p))];
    const fanOutTop10 = pct(fanOutValues, 0.9);
    const fanInBottom25 = pct(fanInValues, 0.25);

    const ENTRY_FILENAMES = new Set([
      'index.ts', 'index.js', 'main.ts', 'main.js', 'app.ts', 'app.js', 'server.ts', 'server.js',
      'mod.rs', 'main.go', 'main.py', 'main.rs', 'manage.py', 'app.py', 'wsgi.py', 'asgi.py',
      'run.py', '__main__.py', 'Application.java', 'Main.java', 'Program.cs', 'config.ru',
      'index.php', 'App.swift', 'Application.kt', 'main.cpp', 'main.c'
    ]);

    function depthOfPath(filePath) {
      if (!filePath) return 99;
      const norm = filePath.replace(/\\/g, '/');
      return norm.split('/').length - 1; // 0 = root, 1 = one level deep
    }

    const entryScores = [];
    for (const n of nodes) {
      let score = 0;
      if (n.type === 'file') {
        if (ENTRY_FILENAMES.has(n.name)) score += 3;
        const d = depthOfPath(n.filePath);
        if (d <= 1) score += 1;
        if ((fanOut.get(n.id) || 0) >= fanOutTop10 && fanOutTop10 > 0) score += 1;
        if ((fanIn.get(n.id) || 0) <= fanInBottom25) score += 1;
      } else if (n.type === 'document') {
        const d = depthOfPath(n.filePath);
        if (n.name === 'README.md' && d === 0) score += 5;
        else if (d === 0 && n.filePath && n.filePath.toLowerCase().endsWith('.md')) score += 2;
      }
      if (score > 0) entryScores.push({ id: n.id, score, name: n.name, summary: n.summary });
    }
    entryScores.sort((a, b) => b.score - a.score);
    const entryPointCandidates = entryScores.slice(0, 5);

    // BFS from top code entry point (skip documents)
    const topCodeEntry = entryScores.find(e => nodeById.get(e.id).type !== 'document');
    const bfsEdgeTypes = new Set(['imports', 'calls']);
    const adjForward = new Map();
    for (const n of nodes) adjForward.set(n.id, []);
    for (const e of validEdges) {
      if (bfsEdgeTypes.has(e.type)) {
        adjForward.get(e.source).push(e.target);
      }
    }

    let bfsTraversal = { startNode: null, order: [], depthMap: {}, byDepth: {} };
    if (topCodeEntry) {
      const start = topCodeEntry.id;
      const visited = new Set([start]);
      const order = [start];
      const depthMap = { [start]: 0 };
      const queue = [start];
      while (queue.length) {
        const cur = queue.shift();
        const d = depthMap[cur];
        for (const next of (adjForward.get(cur) || [])) {
          if (!visited.has(next)) {
            visited.add(next);
            depthMap[next] = d + 1;
            order.push(next);
            queue.push(next);
          }
        }
      }
      const byDepth = {};
      for (const [id, d] of Object.entries(depthMap)) {
        byDepth[d] = byDepth[d] || [];
        byDepth[d].push(id);
      }
      bfsTraversal = { startNode: start, order, depthMap, byDepth };
    }

    // Non-code file inventory
    const nonCodeFiles = { documentation: [], infrastructure: [], data: [], config: [] };
    for (const n of nodes) {
      const entry = { id: n.id, name: n.name, type: n.type, summary: n.summary };
      if (n.type === 'document') nonCodeFiles.documentation.push(entry);
      else if (['service', 'pipeline', 'resource'].includes(n.type)) nonCodeFiles.infrastructure.push(entry);
      else if (['table', 'schema', 'endpoint'].includes(n.type)) nonCodeFiles.data.push(entry);
      else if (n.type === 'config') nonCodeFiles.config.push(entry);
    }

    // Tightly coupled clusters: bidirectional relationships expanded
    const edgeKey = (a, b) => a + '=>' + b;
    const edgeSet = new Set(validEdges.map(e => edgeKey(e.source, e.target)));
    const pairEdgeCount = new Map();
    for (const e of validEdges) {
      const k = [e.source, e.target].sort().join('|');
      pairEdgeCount.set(k, (pairEdgeCount.get(k) || 0) + 1);
    }

    const uf = new Map();
    function find(x) { if (!uf.has(x)) uf.set(x, x); let r = x; while (uf.get(r) !== r) r = uf.get(r); uf.set(x, r); return r; }
    function union(a, b) { const ra = find(a), rb = find(b); if (ra !== rb) uf.set(ra, rb); }

    const bidirPairs = [];
    for (const e of validEdges) {
      if (edgeSet.has(edgeKey(e.target, e.source))) {
        bidirPairs.push([e.source, e.target]);
        union(e.source, e.target);
      }
    }

    const groups = new Map();
    for (const [a, b] of bidirPairs) {
      const root = find(a);
      if (!groups.has(root)) groups.set(root, new Set());
      groups.get(root).add(a);
      groups.get(root).add(b);
    }

    // Expand: add nodes connecting to 2+ existing cluster members
    for (const [root, members] of groups.entries()) {
      let changed = true;
      let iterations = 0;
      while (changed && iterations < 5 && members.size < 5) {
        changed = false;
        iterations++;
        const connectionCount = new Map();
        for (const e of validEdges) {
          if (members.has(e.target) && !members.has(e.source)) {
            connectionCount.set(e.source, (connectionCount.get(e.source) || 0) + 1);
          }
          if (members.has(e.source) && !members.has(e.target)) {
            connectionCount.set(e.target, (connectionCount.get(e.target) || 0) + 1);
          }
        }
        for (const [cand, cnt] of connectionCount.entries()) {
          if (cnt >= 2 && members.size < 5) {
            members.add(cand);
            changed = true;
          }
        }
      }
    }

    let clusters = [];
    for (const members of groups.values()) {
      const nodeList = [...members].slice(0, 5);
      if (nodeList.length >= 2) {
        let edgeCount = 0;
        for (const e of validEdges) {
          if (members.has(e.source) && members.has(e.target)) edgeCount++;
        }
        clusters.push({ nodes: nodeList, edgeCount });
      }
    }
    // dedupe identical clusters
    const seenClusterKeys = new Set();
    clusters = clusters.filter(c => {
      const key = [...c.nodes].sort().join('|');
      if (seenClusterKeys.has(key)) return false;
      seenClusterKeys.add(key);
      return true;
    });
    clusters.sort((a, b) => b.edgeCount - a.edgeCount);
    clusters = clusters.slice(0, 10);

    const nodeSummaryIndex = {};
    for (const n of nodes) {
      nodeSummaryIndex[n.id] = { name: n.name, type: n.type, summary: n.summary };
    }

    const result = {
      scriptCompleted: true,
      entryPointCandidates,
      fanInRanking,
      fanOutRanking,
      bfsTraversal,
      nonCodeFiles,
      clusters,
      layers: { count: layers.length, list: layers },
      nodeSummaryIndex,
      totalNodes: nodes.length,
      totalEdges: edges.length
    };

    fs.writeFileSync(outputPath, JSON.stringify(result, null, 2), 'utf-8');
    process.exit(0);
  } catch (e) {
    console.error('Fatal error during analysis: ' + (e && e.stack ? e.stack : e));
    process.exit(1);
  }
}

main();
