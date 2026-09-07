const fs = require('fs');
const path = require('path');

function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!inputPath || !outputPath) {
    console.error('Usage: node ua-arch-analyze.js <input.json> <output.json>');
    process.exit(1);
  }
  const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const { fileNodes, importEdges, allEdges } = input;

  const nodeById = new Map(fileNodes.map(n => [n.id, n]));

  // A. Directory grouping
  function dirOf(filePath) {
    const parts = filePath.split('/');
    parts.pop();
    return parts.join('/');
  }

  const allPaths = fileNodes.map(n => n.filePath || n.name || '');
  // compute common prefix (directory-based)
  function commonPrefix(paths) {
    if (paths.length === 0) return '';
    const splitPaths = paths.map(p => p.split('/'));
    const minLen = Math.min(...splitPaths.map(p => p.length));
    let prefix = [];
    for (let i = 0; i < minLen - 1; i++) { // -1 to leave at least the filename
      const seg = splitPaths[0][i];
      if (splitPaths.every(p => p[i] === seg)) {
        prefix.push(seg);
      } else {
        break;
      }
    }
    return prefix.length ? prefix.join('/') + '/' : '';
  }

  const prefix = commonPrefix(allPaths);

  function groupFor(filePath) {
    let rel = filePath.startsWith(prefix) ? filePath.slice(prefix.length) : filePath;
    const parts = rel.split('/');
    if (parts.length > 1) {
      return parts[0];
    }
    // flat file directly under prefix (or root) - group by extension pattern
    const fname = parts[0];
    if (/\.test\.|\.spec\.|^test_|_test\.go$|Test\.java$|_spec\.rb$|Test\.php$|Tests\.cs$/.test(fname)) return 'test';
    if (/\.config\./.test(fname)) return 'config';
    const ext = fname.includes('.') ? fname.split('.').pop() : '(noext)';
    return prefix ? prefix.replace(/\/$/, '') : `(root-${ext})`;
  }

  const directoryGroups = {};
  for (const n of fileNodes) {
    const fp = n.filePath || n.name || '';
    const g = groupFor(fp);
    if (!directoryGroups[g]) directoryGroups[g] = [];
    directoryGroups[g].push(n.id);
  }

  // B. Node type grouping
  const nodeTypeGroups = {};
  for (const n of fileNodes) {
    if (!nodeTypeGroups[n.type]) nodeTypeGroups[n.type] = [];
    nodeTypeGroups[n.type].push(n.id);
  }

  // C. Import adjacency
  const fanOut = {};
  const fanIn = {};
  const importAdj = {}; // source -> [targets]
  for (const e of importEdges) {
    if (e.type !== 'imports') continue;
    fanOut[e.source] = (fanOut[e.source] || 0) + 1;
    fanIn[e.target] = (fanIn[e.target] || 0) + 1;
    if (!importAdj[e.source]) importAdj[e.source] = [];
    importAdj[e.source].push(e.target);
  }

  // group lookup
  const idToGroup = {};
  for (const [g, ids] of Object.entries(directoryGroups)) {
    for (const id of ids) idToGroup[id] = g;
  }

  // D. Cross-category dependency analysis (using allEdges, non-import edges typically)
  const crossCategoryMap = {};
  for (const e of allEdges) {
    const s = nodeById.get(e.source);
    const t = nodeById.get(e.target);
    if (!s || !t) continue;
    if (s.type === t.type && e.type === 'imports') continue; // handled elsewhere but still could count
    const key = `${s.type}|${t.type}|${e.type}`;
    crossCategoryMap[key] = (crossCategoryMap[key] || 0) + 1;
  }
  const crossCategoryEdges = Object.entries(crossCategoryMap).map(([k, count]) => {
    const [fromType, toType, edgeType] = k.split('|');
    return { fromType, toType, edgeType, count };
  }).sort((a,b) => b.count - a.count);

  // E. Inter-group import frequency
  const interGroupMap = {};
  for (const e of importEdges) {
    if (e.type !== 'imports') continue;
    const sg = idToGroup[e.source];
    const tg = idToGroup[e.target];
    if (!sg || !tg || sg === tg) continue;
    const key = `${sg}|${tg}`;
    interGroupMap[key] = (interGroupMap[key] || 0) + 1;
  }
  const interGroupImports = Object.entries(interGroupMap).map(([k, count]) => {
    const [from, to] = k.split('|');
    return { from, to, count };
  }).sort((a,b) => b.count - a.count);

  // F. Intra-group density
  const intraGroupDensity = {};
  for (const g of Object.keys(directoryGroups)) {
    let internalEdges = 0;
    let totalEdges = 0;
    for (const e of importEdges) {
      if (e.type !== 'imports') continue;
      const sg = idToGroup[e.source];
      const tg = idToGroup[e.target];
      if (sg === g && tg === g) { internalEdges++; totalEdges++; }
      else if (sg === g || tg === g) { totalEdges++; }
    }
    intraGroupDensity[g] = {
      internalEdges,
      totalEdges,
      density: totalEdges > 0 ? +(internalEdges / totalEdges).toFixed(3) : 0
    };
  }

  // G. Directory pattern matching
  const patternTable = [
    [['routes','api','controllers','endpoints','handlers'], 'api'],
    [['services','core','lib','domain','logic'], 'service'],
    [['models','db','data','persistence','repository','entities'], 'data'],
    [['components','views','pages','ui','layouts','screens'], 'ui'],
    [['middleware','plugins','interceptors','guards'], 'middleware'],
    [['utils','helpers','common','shared','tools'], 'utility'],
    [['config','constants','env','settings'], 'config'],
    [['__tests__','test','tests','spec','specs'], 'test'],
    [['types','interfaces','schemas','contracts','dtos'], 'types'],
    [['hooks'], 'hooks'],
    [['store','state','reducers','actions','slices'], 'state'],
    [['assets','static','public'], 'assets'],
    [['migrations'], 'data'],
    [['management','commands'], 'config'],
    [['templatetags'], 'utility'],
    [['signals'], 'service'],
    [['serializers'], 'api'],
    [['cmd'], 'entry'],
    [['internal'], 'service'],
    [['pkg'], 'utility'],
    [['dto','request','response'], 'types'],
    [['entity'], 'data'],
    [['controller'], 'api'],
    [['routers'], 'api'],
    [['composables'], 'service'],
    [['blueprints'], 'api'],
    [['mailers','jobs','channels'], 'service'],
    [['bin'], 'entry'],
    [['docs','documentation','wiki'], 'documentation'],
    [['deploy','deployment','infra','infrastructure'], 'infrastructure'],
    [['.github','.gitlab','.circleci'], 'ci-cd'],
    [['k8s','kubernetes','helm','charts'], 'infrastructure'],
    [['terraform','tf'], 'infrastructure'],
    [['docker'], 'infrastructure'],
    [['sql','database','schema'], 'data'],
    [['benchmarks'], 'test'],
    [['deliverables'], 'documentation'],
  ];

  const patternMatches = {};
  for (const g of Object.keys(directoryGroups)) {
    const lower = g.toLowerCase();
    let matched = null;
    for (const [dirs, label] of patternTable) {
      if (dirs.includes(lower)) { matched = label; break; }
    }
    if (matched) patternMatches[g] = matched;
  }

  // H. Deployment topology
  const infraFiles = [];
  let hasDockerfile = false, hasCompose = false, hasK8s = false, hasTerraform = false, hasCI = false;
  for (const n of fileNodes) {
    const fp = n.filePath || '';
    const base = path.basename(fp);
    if (/^Dockerfile/.test(base)) { hasDockerfile = true; infraFiles.push(fp); }
    if (/docker-compose/.test(base)) { hasCompose = true; infraFiles.push(fp); }
    if (/\.ya?ml$/.test(base) && /k8s|kubernetes/i.test(fp)) { hasK8s = true; infraFiles.push(fp); }
    if (/\.tf$|\.tfvars$/.test(base)) { hasTerraform = true; infraFiles.push(fp); }
    if (/^\.github\/workflows\//.test(fp) || base === '.gitlab-ci.yml' || base === 'Jenkinsfile') { hasCI = true; infraFiles.push(fp); }
    if (base === 'Makefile' || base === 'run.ps1') { infraFiles.push(fp); }
  }

  const deploymentTopology = {
    hasDockerfile, hasCompose, hasK8s, hasTerraform, hasCI,
    infraFiles: [...new Set(infraFiles)]
  };

  // I. Data pipeline detection
  const schemaFiles = [];
  const migrationFiles = [];
  const dataModelFiles = [];
  const apiHandlerFiles = [];
  for (const n of fileNodes) {
    const fp = n.filePath || '';
    if (/\.sql$/.test(fp)) {
      if (/migrations?\//.test(fp)) migrationFiles.push(fp);
      else schemaFiles.push(fp);
    }
    if (/\.graphql$|\.proto$|\.gql$/.test(fp)) schemaFiles.push(fp);
    if (n.type === 'table' || n.type === 'schema') schemaFiles.push(n.id);
    if (/models?\/|db\.py$|persistence/.test(fp)) dataModelFiles.push(fp);
    if (n.type === 'endpoint' || /routes|controllers|handlers|main\.py$/.test(fp)) apiHandlerFiles.push(fp || n.id);
  }

  const dataPipeline = {
    schemaFiles: [...new Set(schemaFiles)],
    migrationFiles: [...new Set(migrationFiles)],
    dataModelFiles: [...new Set(dataModelFiles)],
    apiHandlerFiles: [...new Set(apiHandlerFiles)]
  };

  // J. Documentation coverage
  const docFiles = fileNodes.filter(n => n.type === 'document' || /\.md$|\.rst$/.test(n.filePath || ''));
  const groupsWithDocsSet = new Set();
  for (const doc of docFiles) {
    const fp = (doc.filePath || '').toLowerCase();
    for (const g of Object.keys(directoryGroups)) {
      if (fp.includes(g.toLowerCase())) groupsWithDocsSet.add(g);
    }
    if (/readme/i.test(fp)) {
      const dirg = groupFor(doc.filePath || '');
      groupsWithDocsSet.add(dirg);
    }
  }
  const totalGroups = Object.keys(directoryGroups).length;
  const groupsWithDocs = groupsWithDocsSet.size;
  const undocumentedGroups = Object.keys(directoryGroups).filter(g => !groupsWithDocsSet.has(g));

  const docCoverage = {
    groupsWithDocs,
    totalGroups,
    coverageRatio: totalGroups > 0 ? +(groupsWithDocs / totalGroups).toFixed(2) : 0,
    undocumentedGroups
  };

  // K. Dependency direction
  const dependencyDirection = [];
  const seenPairs = new Set();
  for (const { from, to, count } of interGroupImports) {
    const reverseKey = `${to}|${from}`;
    const forwardKey = `${from}|${to}`;
    if (seenPairs.has(forwardKey) || seenPairs.has(reverseKey)) continue;
    const reverseCount = interGroupMap[reverseKey] || 0;
    if (count > reverseCount) {
      dependencyDirection.push({ dependent: from, dependsOn: to });
    } else if (reverseCount > count) {
      dependencyDirection.push({ dependent: to, dependsOn: from });
    }
    seenPairs.add(forwardKey);
    seenPairs.add(reverseKey);
  }

  // fileStats
  const filesPerGroup = {};
  for (const [g, ids] of Object.entries(directoryGroups)) filesPerGroup[g] = ids.length;
  const nodeTypeCounts = {};
  for (const [t, ids] of Object.entries(nodeTypeGroups)) nodeTypeCounts[t] = ids.length;

  const result = {
    scriptCompleted: true,
    directoryGroups,
    nodeTypeGroups,
    crossCategoryEdges,
    interGroupImports,
    intraGroupDensity,
    patternMatches,
    deploymentTopology,
    dataPipeline,
    docCoverage,
    dependencyDirection,
    fileStats: {
      totalFileNodes: fileNodes.length,
      filesPerGroup,
      nodeTypeCounts
    },
    fileFanIn: fanIn,
    fileFanOut: fanOut
  };

  fs.writeFileSync(outputPath, JSON.stringify(result, null, 2));
  console.log('Analysis complete. Output written to', outputPath);
}

try {
  main();
  process.exit(0);
} catch (err) {
  console.error('Fatal error:', err.stack || err);
  process.exit(1);
}
