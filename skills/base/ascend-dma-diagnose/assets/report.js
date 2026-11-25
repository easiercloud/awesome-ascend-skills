(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const viewport = document.getElementById('graph-viewport');
  const status = document.getElementById('graph-status');
  const pages = (data.focus && data.focus.pages) || [];
  const navigator = document.getElementById('root-navigation');
  let current = 'machine';
  let rootIndex = 0;
  let scale = 1;
  function active() { return document.querySelector('[data-graph="' + current + '"]'); }
  function zoom(value) {
    const stage = active(), svg = stage.querySelector('svg');
    if (!svg) return;
    scale = Math.max(.08, Math.min(3, value));
    svg.style.transform = 'scale(' + scale + ')';
    stage.style.width = Number(svg.getAttribute('width')) * scale + 'px';
    stage.style.height = Number(svg.getAttribute('height')) * scale + 'px';
  }
  function fit() {
    const svg = active().querySelector('svg');
    if (svg) zoom((viewport.clientWidth - 12) / Number(svg.getAttribute('width')));
  }
  function describe() {
    if (current === 'machine') {
      const g = data.graphs.machine;
      status.textContent = '整机 I/O · NPU ' + g.npu_bdfs.length + ' / 祖先 ' + g.context_bdfs.length + ' · ' + g.exact_parent_edges.length + ' 条实际 NPU 父边 · NIC ' + g.nic_bdfs.length + ' / NVMe ' + g.nvme_bdfs.length + ' 精确名录 · 完整来源 ' + g.source_pci_nodes + ' functions / ' + g.source_pci_edges + ' 父边';
    } else if (current === 'focus') {
      const page = pages[rootIndex];
      status.textContent = '关键端点 · RootBus ' + page.root_bus + ' · 本页 ' + page.members.length + '/' + page.source_members.length + ' 个本根 functions · ' + page.graph.rendered_edges + ' 条真实父子边 · 完整来源 ' + data.focus.source_pci_nodes + ' functions / ' + data.focus.source_pci_edges + ' 条父子边';
    } else {
      const g = data.graphs[current];
      status.textContent = (current === 'full' ? '完整树' : '精确概览') + ' · ' + g.visible_pci_nodes + '/' + g.pci_nodes + ' PCI functions 可见 · ' + g.rendered_edges + ' 条父子边' + (g.grouped_pci_nodes ? ' · 精确分组 ' + g.grouped_pci_nodes + ' 个 Host/Other 叶子函数，成员见下方' : '');
    }
  }
  function setView(view) {
    current = view === 'focus' && !pages.length ? 'overview' : view;
    document.querySelectorAll('[data-graph]').forEach(el => { el.hidden = el.dataset.graph !== current; });
    document.querySelectorAll('button[data-view]').forEach(el => {
      el.setAttribute('aria-pressed', String(el.dataset.view === current));
      if (el.dataset.view === 'focus') el.disabled = !pages.length;
    });
    navigator.hidden = current !== 'focus';
    viewport.classList.toggle('machine-view', current === 'machine');
    describe(); fit(); viewport.scrollTo({left:0, top:0});
  }
  function setRoot(index) {
    if (!pages.length) return;
    rootIndex = Math.max(0, Math.min(pages.length - 1, Number(index) || 0));
    const page = pages[rootIndex];
    const template = document.getElementById('focus-template-' + page.id);
    document.querySelector('[data-graph="focus"]').replaceChildren(template.content.cloneNode(true));
    document.getElementById('focus-root').value = String(rootIndex);
    document.getElementById('root-prev').disabled = rootIndex === 0;
    document.getElementById('root-next').disabled = rootIndex === pages.length - 1;
    document.getElementById('root-page-number').textContent = (rootIndex + 1) + ' / ' + pages.length;
    document.getElementById('root-download').setAttribute('href', page.path);
    setView('focus');
  }
  function openDetails(id) {
    const target = document.getElementById(id);
    if (!target) return;
    let p = target;
    while (p) { if (p.tagName === 'DETAILS') p.open = true; p = p.parentElement; }
    target.scrollIntoView({behavior:'smooth', block:'center'});
  }
  function locate(found) {
    const pageIndex = pages.findIndex(page => page.members.includes(found.bdf));
    if (pageIndex >= 0) setRoot(pageIndex); else setView('full');
    zoom(Math.max(scale, .75));
    const node = Array.from(active().querySelectorAll('[data-bdf]')).find(el => el.dataset.bdf === found.bdf);
    if (!node) return;
    document.querySelectorAll('.search-hit').forEach(el => el.classList.remove('search-hit'));
    node.classList.add('search-hit');
    viewport.scrollTo({left:Math.max(0, Number(node.dataset.x)*scale-70), top:Math.max(0, Number(node.dataset.y)*scale-50), behavior:'smooth'});
    status.textContent = '已定位 ' + found.bdf + (pageIndex < 0 ? ' · 省略成员已在完整树打开' : ' · 已切到对应根总线页') + ' · 点击节点查看详情与证据';
  }
  function search() {
    const query = document.getElementById('topology-search').value.trim().toLowerCase();
    if (!query) return;
    const found = data.search.find(item => item.bdf.toLowerCase() === query) || data.search.find(item => item.text.toLowerCase().includes(query));
    if (!found) { status.textContent = '未找到：' + query; return; }
    locate(found);
  }
  document.querySelectorAll('button[data-view]').forEach(el => el.addEventListener('click', () => setView(el.dataset.view)));
  document.getElementById('focus-root').addEventListener('change', event => setRoot(event.target.value));
  document.getElementById('root-prev').addEventListener('click', () => setRoot(rootIndex-1));
  document.getElementById('root-next').addEventListener('click', () => setRoot(rootIndex+1));
  document.getElementById('zoom-in').addEventListener('click', () => zoom(scale*1.25));
  document.getElementById('zoom-out').addEventListener('click', () => zoom(scale/1.25));
  document.getElementById('zoom-fit').addEventListener('click', fit);
  document.getElementById('search-button').addEventListener('click', search);
  document.getElementById('topology-search').addEventListener('keydown', event => { if(event.key==='Enter') search(); });
  viewport.addEventListener('click', event => {
    const anchor = event.target.closest('a');
    const href = anchor && anchor.getAttribute('href');
    if (current === 'machine' && anchor && anchor.dataset.bdf) {
      event.preventDefault(); locate({bdf:anchor.dataset.bdf}); return;
    }
    if (href && href.startsWith('#')) { event.preventDefault(); openDetails(href.slice(1)); }
  });
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[href^="#ev-"]');
    if (anchor) openDetails(anchor.getAttribute('href').slice(1));
  });
  window.addEventListener('hashchange', () => openDetails(location.hash.slice(1)));
  if (pages.length) setRoot(0);
  setView('machine');
  if(location.hash) openDetails(location.hash.slice(1));
})();
