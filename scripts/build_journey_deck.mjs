/** Build the two-slide native editable deck from build_diagram.py's scenes.
 * Uses the supplied @oai/artifact-tool runtime. No images are embedded as slides.
 * Set RUNTIME_NODE_MODULES, RUNTIME_PYTHON and PRESENTATIONS_SKILL_DIR to the
 * installed authoring runtime. Run build_diagram.py first after changing copy.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const workspaceDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const required = ['RUNTIME_NODE_MODULES', 'RUNTIME_PYTHON', 'PRESENTATIONS_SKILL_DIR'];
const missing = required.filter(name => !process.env[name]);
if (missing.length) {
  throw new Error(`Optional PowerPoint authoring requires configured runtime paths: ${missing.join(', ')}. See README.md. To rebuild SVG/PNG, use scripts/build_diagram.py.`);
}
const runtimeModules = path.resolve(process.env.RUNTIME_NODE_MODULES);
const runtimePython = path.resolve(process.env.RUNTIME_PYTHON);
const skillDir = path.resolve(process.env.PRESENTATIONS_SKILL_DIR);
process.env.RUNTIME_NODE_MODULES = runtimeModules;
const runtimeRequire = createRequire(path.join(runtimeModules, '__runtime__.cjs'));
const { Presentation, PresentationFile, FileBlob } = await import(pathToFileURL(runtimeRequire.resolve('@oai/artifact-tool')).href);
const { finalizePresentation } = await import(pathToFileURL(path.join(skillDir, 'container_tools/artifact_tool_utils.mjs')).href);
const scenes = await Promise.all(['user-journey', 'dots-duet-context'].map(async stem => ({
  stem, ...JSON.parse(await fs.readFile(path.join(workspaceDir, 'docs/diagrams', stem + '.scene.json'), 'utf8')),
})));
const { width, height } = scenes[0];
const presentation = Presentation.create({ slideSize: { width, height } });
const buildId = new Date().toISOString().replace(/[^0-9]/g, '');
const privateDir = path.join(workspaceDir, '.codex-finalizer', buildId);
const validatedDir = path.join(workspaceDir, '.diagram-pptx-output', buildId);
await fs.mkdir(privateDir, { recursive: true });
await fs.mkdir(validatedDir, { recursive: true });

function lineStyle(p) {
  return { style: p.dashed ? 'dashed' : 'solid', fill: p.color || p.stroke || 'none', width: p.width || 0 };
}

for (const [slideIndex, scene] of scenes.entries()) {
  const slide = presentation.slides.add();
  slide.background.fill = scene.background;
  slide.speakerNotes.text = slideIndex === 0
    ? 'Целевой пользовательский путь по исходному запросу и текущему локальному репозиторию. Цвет кромки показывает исполнителя: код файлового пути, модель/пользователь или инструменты выбранного сервиса. Схема не подтверждает живой доступ ко всем перечисленным сервисам. Нативный импорт и облачные действия проверяются отдельно.'
    : 'Состав переносимого контекста Dots/Duet задан пользователем. Это требование к полноте данных, а не утверждение о наличии универсального API. Исходные инструкции сохраняются вместе с источником и областью действия. Подтверждение внешних действий требует фактического результата инструмента.';
  for (const [index, p] of scene.primitives.entries()) {
    const name = `${scene.stem}/${p.group || 'page'}/${p.type}-${index}`;
    if (p.type === 'text') {
      if (![18, 24].includes(p.size)) throw new Error('Only the two requested font sizes are allowed');
      const shape = slide.shapes.add({ geometry: 'textbox', name,
        position: { left: p.x, top: p.y-3, width: p.w+20, height: p.size+14 },
        fill: 'none', line: { fill: 'none', width: 0 },
      });
      shape.text = p.text;
      shape.text.style = { typeface: 'Segoe UI', fontSize: p.size, bold: p.size === 24,
        color: p.color, alignment: 'left', verticalAlignment: 'top',
        autoFit: 'none', wrap: 'none', insets: 0 };
    } else if (p.type === 'rect') {
      slide.shapes.add({ geometry: 'roundRect', name,
        position: { left: p.x, top: p.y, width: p.w, height: p.h },
        fill: p.fill, line: lineStyle(p), borderRadius: p.radius,
      });
    } else if (p.type === 'circle') {
      slide.shapes.add({ geometry: 'ellipse', name,
        position: { left: p.x-p.radius, top: p.y-p.radius, width: p.radius*2, height: p.radius*2 },
        fill: p.fill, line: lineStyle(p),
      });
    } else if (p.type === 'diamond') {
      slide.shapes.add({ geometry: 'diamond', name,
        position: { left: p.x, top: p.y, width: p.w, height: p.h }, fill: p.fill, line: lineStyle(p),
      });
    } else if (p.type === 'path') {
      const segments = p.points.slice(1).map((point, k) => [p.points[k], point])
        .filter(([a,b]) => a[0] !== b[0] || a[1] !== b[1]);
      for (const [k, [a,b]] of segments.entries()) {
        const connector = { geometry: 'connector', kind: 'straight', name: `${name}/segment-${k}`,
          position: { left: Math.min(a[0],b[0]), top: Math.min(a[1],b[1]),
            width: Math.abs(b[0]-a[0]), height: Math.abs(b[1]-a[1]),
            horizontalFlip: b[0] < a[0], verticalFlip: b[1] < a[1] },
          line: lineStyle(p),
        };
        if (p.arrow && k === segments.length-1) connector.tail = { type: 'triangle', width: 'sm', length: 'sm' };
        slide.shapes.add(connector);
      }
    } else throw new Error('Unknown scene primitive: ' + p.type);
  }
}

const draft = path.join(privateDir, 'candidate.pptx');
await (await PresentationFile.exportPptx(presentation)).save(draft);
console.log(JSON.stringify({ stage: 'exported_draft', slides: scenes.length, candidate: draft }));
const validated = path.join(validatedDir, 'Путь переноса памяти.pptx');
await finalizePresentation({
  workspaceDir, candidatePath: draft, finalPath: validated,
  explicitTotalSlideCount: 2, requiredNativeTableOwnerSlides: [], requiredNativeChartOwnerSlides: [],
  pythonExecutable: runtimePython,
  integrityValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_package_integrity.py'),
  layoutValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_layout_geometry.py'),
  layoutArgs: ['--expected-slide-size-emu', `${width*9525},${height*9525}`, '--validate-bullet-geometry', '--validate-heading-fit'],
  fontPolicy: { basis: 'design', families: ['Segoe UI'] },
  verifyArtifactToolImport: true,
  receiptPath: path.join(privateDir, 'validation.json'),
});
console.log(JSON.stringify({ stage: 'validated', file: validated }));

// Render the exported final deck, not a bitmap approximation of its objects.
const finalPresentation = await PresentationFile.importPptx(await FileBlob.load(validated));
const outputDeck = path.join(workspaceDir, 'Путь переноса памяти.pptx');
const outputNames = ['Путь переноса памяти.png', 'Контекст Dots и Duet.png'];
for (const [index, slide] of finalPresentation.slides.items.entries()) {
  const image = await finalPresentation.export({ slide, format: 'png', scale: 1 });
  const bytes = new Uint8Array(await image.arrayBuffer());
  await fs.writeFile(path.join(privateDir, `slide-${index+1}.png`), bytes);
  await fs.writeFile(path.join(workspaceDir, outputNames[index]), bytes);
}
await fs.copyFile(validated, outputDeck);
console.log(JSON.stringify({ pptx: outputDeck, previews: outputNames.map(name => path.join(workspaceDir,name)),
  validation: path.join(privateDir,'validation.json'), native_editable: true, slides: 2, font_sizes_px: [24,18] }));
