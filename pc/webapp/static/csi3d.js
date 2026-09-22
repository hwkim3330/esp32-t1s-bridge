// 3D view of the CSI links. Reads like a LIDAR viewport on purpose, but it is
// carefully NOT a point cloud, because the measurement cannot support one.
//
// What is drawn, and why each part is honest:
//
//   * Board markers and the floor grid in metres -- the PHYSICAL LAYOUT the
//     boards were placed in. Known, not measured. Labelled as such on the
//     page so nobody reads the grid as range returns.
//   * A beam per radio path -- these exist. Its colour and glow are that
//     path's live CSI amplitude variance, the one thing actually measured.
//   * A translucent ellipsoid hugging each beam -- the link's first Fresnel
//     zone, r = sqrt(lambda*d1*d2/(d1+d2)), lambda = 12.5 cm at 2.4 GHz.
//     This is the volume a body has to occupy to move that path's number,
//     so drawing it to scale turns "which path is disturbed" into "where in
//     the room", which is as much localisation as three paths can give.
//
// There are deliberately NO range rings and NO detected points: range
// resolution here is c/2B with B one 20 MHz channel, i.e. 7.5 m, so the whole
// room is a single range bin. Drawing a sweep with rings would imply a
// precision the physics does not support.
(function () {
  "use strict";

  // Physical placement, in metres. Edit to match the bench if the boards move.
  const BOARDS = {
    "esp32-1": { pos: [0.0, 0.35, -1.6], label: "ESP32-1" },
    "esp32-2": { pos: [-1.4, 0.35, 0.9], label: "ESP32-2" },
    "esp32-3": { pos: [1.4, 0.35, 0.9], label: "ESP32-3" },
  };
  const LAMBDA = 0.1249; // 2.4 GHz, metres

  // Occupancy field. Three paths give three bits, and the bits are not
  // independent: the centre of the triangle lies inside all three Fresnel
  // zones, a point near one edge inside only that one, a point near a board
  // inside the two that touch it. So "which paths are disturbed" narrows a
  // body down to a region, and the regions are distinguishable. That is real
  // localisation -- by intersection of sensing volumes, not by ranging --
  // and it is the honest way to draw "something is there".
  const FIELD_N = 56;          // cells per side
  const FIELD_SPAN = 5.0;      // metres covered, centred on the origin
  const FIELD_H = 0.35;        // evaluated at path height: where a standing
                               // body actually intersects the zones
  const SHOW_AT = 0.25;        // below this, nothing is drawn rather than
                               // dressing up noise as a detection
  let fieldTex = null, fieldData = null, fieldPlane = null, sens = {}, peakRing = null;

  let renderer, scene, camera, root, raf = null;
  let beams = {}, zones = {}, markers = {};
  let latest = { links: [], nodes: {} };

  function labelSprite(text) {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 64;
    const g = c.getContext("2d");
    g.font = "600 34px Inter, system-ui, sans-serif";
    g.fillStyle = "#dfe7f2";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(text, 128, 32);
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
    sp.scale.set(0.9, 0.225, 1);
    return sp;
  }

  function pairKey(a, b) { return [a, b].sort().join("|"); }

  function buildPaths() {
    const ids = Object.keys(BOARDS);
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = BOARDS[ids[i]].pos, b = BOARDS[ids[j]].pos;
        const A = new THREE.Vector3(...a), B = new THREE.Vector3(...b);
        const mid = A.clone().add(B).multiplyScalar(0.5);
        const d = A.distanceTo(B);
        // First Fresnel zone at the midpoint, where it is widest.
        const r = Math.sqrt((LAMBDA * (d / 2) * (d / 2)) / d);

        const beam = new THREE.Mesh(
          new THREE.CylinderGeometry(0.016, 0.016, d, 12, 1, true),
          new THREE.MeshBasicMaterial({ color: 0x3ba7ff, transparent: true, opacity: 0.85 })
        );
        const zone = new THREE.Mesh(
          new THREE.SphereGeometry(1, 28, 18),
          new THREE.MeshBasicMaterial({ color: 0x3ba7ff, transparent: true, opacity: 0.05,
                                        depthWrite: false, side: THREE.DoubleSide })
        );
        zone.scale.set(r, d / 2, r);

        // Cylinders and spheres are built along +Y, so rotate that axis onto
        // the path rather than hand-rolling a lookAt with an up-vector that
        // degenerates when a path happens to be vertical.
        const dir = B.clone().sub(A).normalize();
        const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
        for (const m of [beam, zone]) { m.position.copy(mid); m.quaternion.copy(q); root.add(m); }

        const key = pairKey(ids[i], ids[j]);
        beams[key] = beam; zones[key] = zone;
      }
    }
  }

  // Sensitivity of one path to a body at cell (x, z): how deep inside that
  // link's first Fresnel zone the point is. Zero at the endpoints, widest at
  // the midpoint, falling off as a Gaussian in the perpendicular distance.
  function buildSensitivity() {
    const ids = Object.keys(BOARDS);
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const A = new THREE.Vector3(...BOARDS[ids[i]].pos);
        const B = new THREE.Vector3(...BOARDS[ids[j]].pos);
        const AB = B.clone().sub(A);
        const L = AB.length();
        const map = new Float32Array(FIELD_N * FIELD_N);
        const P = new THREE.Vector3(), C = new THREE.Vector3();
        for (let zi = 0; zi < FIELD_N; zi++) {
          for (let xi = 0; xi < FIELD_N; xi++) {
            P.set((xi / (FIELD_N - 1) - 0.5) * FIELD_SPAN, FIELD_H,
                  (zi / (FIELD_N - 1) - 0.5) * FIELD_SPAN);
            let t = P.clone().sub(A).dot(AB) / (L * L);
            t = Math.max(0, Math.min(1, t));
            C.copy(A).addScaledVector(AB, t);
            const d1 = t * L, d2 = (1 - t) * L;
            const r = (d1 + d2) > 0 ? Math.sqrt((LAMBDA * d1 * d2) / (d1 + d2)) : 0;
            const dp = P.distanceTo(C);
            map[zi * FIELD_N + xi] = Math.exp(-Math.pow(dp / Math.max(r, 0.08), 2));
          }
        }
        sens[pairKey(ids[i], ids[j])] = map;
      }
    }
  }

  function buildField() {
    fieldData = new Uint8Array(FIELD_N * FIELD_N * 4);
    fieldTex = new THREE.DataTexture(fieldData, FIELD_N, FIELD_N, THREE.RGBAFormat);
    fieldTex.magFilter = THREE.LinearFilter;
    fieldTex.minFilter = THREE.LinearFilter;
    fieldTex.needsUpdate = true;
    fieldPlane = new THREE.Mesh(
      new THREE.PlaneGeometry(FIELD_SPAN, FIELD_SPAN),
      new THREE.MeshBasicMaterial({ map: fieldTex, transparent: true, depthWrite: false,
                                    blending: THREE.AdditiveBlending })
    );
    fieldPlane.rotation.x = -Math.PI / 2;
    fieldPlane.position.y = 0.012;
    fieldPlane.visible = false;
    root.add(fieldPlane);

    peakRing = new THREE.Mesh(
      new THREE.RingGeometry(0.17, 0.23, 40),
      new THREE.MeshBasicMaterial({ color: 0xffc44d, transparent: true, opacity: 0.9,
                                    side: THREE.DoubleSide, depthWrite: false })
    );
    peakRing.rotation.x = -Math.PI / 2;
    peakRing.position.y = 0.02;
    peakRing.visible = false;
    root.add(peakRing);
  }

  // Score a cell against the observed pattern: reward being inside a
  // disturbed path's zone, penalise being inside a QUIET path's zone. The
  // penalty is what makes the centre distinguishable -- without it, the
  // centre scores well whenever any single path is disturbed, and the three
  // bits collapse back into one.
  //
  // The weight has to straddle zero at the SAME threshold the rest of the
  // page calls "disturbed". An earlier version used (f - 0.9*(1-f)), which is
  // negative for anything under f = 0.47: a genuinely disturbed path at
  // f = 0.3 was scored as evidence of absence, every cell came out negative,
  // and the field never drew at all while the text above it said a
  // disturbance had been detected.
  const QUIET = 0.20;      // same threshold the readout uses
  const PENALTY = 1.5;     // absence of disturbance is the stronger evidence
  function updateField(paths) {
    if (!fieldPlane) return null;
    const keys = Object.keys(sens);
    let maxF = 0;
    for (const k of keys) if (paths[k]) maxF = Math.max(maxF, paths[k].f);
    if (maxF < SHOW_AT) {
      fieldPlane.visible = false;
      peakRing.visible = false;
      return null;
    }

    const score = new Float32Array(FIELD_N * FIELD_N);
    let best = -1e9, bestIdx = 0;
    for (let i = 0; i < score.length; i++) {
      let v = 0;
      for (const k of keys) {
        const p = paths[k];
        if (!p) continue;
        const w = p.f - QUIET;
        v += sens[k][i] * (w >= 0 ? w : PENALTY * w);
      }
      score[i] = v;
      if (v > best) { best = v; bestIdx = i; }
    }
    if (best <= 0) {
      fieldPlane.visible = false;
      peakRing.visible = false;
      return null;
    }

    for (let i = 0; i < score.length; i++) {
      const n = Math.max(0, score[i]) / best;
      const a = Math.pow(n, 1.8);
      // amber through white at the peak, the usual "hot" reading
      fieldData[i * 4 + 0] = Math.round(255 * Math.min(1, a * 1.7));
      fieldData[i * 4 + 1] = Math.round(195 * Math.min(1, a * 1.2));
      fieldData[i * 4 + 2] = Math.round(80 * a);
      fieldData[i * 4 + 3] = Math.round(235 * a);
    }
    fieldTex.needsUpdate = true;
    fieldPlane.visible = true;

    const xi = bestIdx % FIELD_N, zi = Math.floor(bestIdx / FIELD_N);
    const px = (xi / (FIELD_N - 1) - 0.5) * FIELD_SPAN;
    const pz = (zi / (FIELD_N - 1) - 0.5) * FIELD_SPAN;
    peakRing.position.set(px, 0.02, pz);
    peakRing.visible = true;
    return { x: px, z: pz, strength: maxF };
  }

  function init(el) {
    if (typeof THREE === "undefined" || renderer) return;
    const w = el.clientWidth || 640, h = 380;

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    renderer.domElement.style.borderRadius = "12px";
    renderer.domElement.style.display = "block";
    host = el;
    el.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1220);
    scene.fog = new THREE.Fog(0x0b1220, 6, 13);

    camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 100);
    root = new THREE.Group();
    scene.add(root);

    // Metre grid: the room the boards sit in, not a range scale.
    const grid = new THREE.GridHelper(8, 16, 0x2b4166, 0x1b2740);
    root.add(grid);

    for (const [id, b] of Object.entries(BOARDS)) {
      const m = new THREE.Mesh(
        new THREE.SphereGeometry(0.08, 20, 14),
        new THREE.MeshBasicMaterial({ color: 0x2fbf6b })
      );
      m.position.set(...b.pos);
      root.add(m);
      const halo = new THREE.Mesh(
        new THREE.SphereGeometry(0.16, 20, 14),
        new THREE.MeshBasicMaterial({ color: 0x2fbf6b, transparent: true, opacity: 0.18 })
      );
      halo.position.copy(m.position);
      root.add(halo);
      const sp = labelSprite(b.label);
      sp.position.set(b.pos[0], b.pos[1] + 0.34, b.pos[2]);
      root.add(sp);
      markers[id] = { dot: m, halo };
    }

    buildPaths();
    buildSensitivity();
    buildField();
    animate();

  }

  let host = null;
  function animate() {
    raf = requestAnimationFrame(animate);
    // The CSI section is display:none when the page first renders, so the
    // container measured 0 wide at init and the canvas got the fallback size.
    // Re-check every frame; it is one property read.
    const want = host && host.clientWidth;
    if (want && Math.abs(want - renderer.domElement.clientWidth) > 2) {
      renderer.setSize(want, 380);
      camera.aspect = want / 380;
      camera.updateProjectionMatrix();
    }
    const t = performance.now() / 1000;
    // Slow orbit: the geometry is three paths in a plane, and a fixed camera
    // flattens two of them into each other.
    const R = 4.6;
    camera.position.set(Math.cos(t * 0.13) * R, 2.5, Math.sin(t * 0.13) * R);
    camera.lookAt(0, 0.3, 0);

    if (peakRing && peakRing.visible) {
      peakRing.scale.setScalar(1 + 0.16 * Math.sin(t * 3.2));
      peakRing.material.opacity = 0.65 + 0.3 * Math.sin(t * 3.2);
    }
    for (const [id, m] of Object.entries(markers)) {
      const alive = latest.nodes?.[id]?.zenohAlive;
      const c = alive ? 0x2fbf6b : 0xd13438;
      m.dot.material.color.setHex(c);
      m.halo.material.color.setHex(c);
      m.halo.scale.setScalar(1 + 0.12 * Math.sin(t * 2 + id.length));
    }
    renderer.render(scene, camera);
  }

  // paths: { "esp32-1|esp32-2": { v, f } } where f is 0..1 disturbance
  // RELATIVE to that path's own recent baseline. Absolute variance is not the
  // signal: a cluttered room sits at a high, steady variance forever, which
  // pins an absolute scale at maximum and shows nothing. What indicates
  // something moving is a path departing from its own quiet level.
  function update(paths, nodes) {
    latest = { nodes: nodes || {} };

    for (const k of Object.keys(beams)) {
      const p = paths ? paths[k] : undefined;
      const seen = p !== undefined;
      const f = seen ? p.f : 0;
      // Calm cyan -> disturbed amber. Hue only; a path that drops out goes
      // grey rather than reading as "calm".
      const col = seen ? new THREE.Color().setHSL(0.55 - 0.44 * f, 0.85, 0.55)
                       : new THREE.Color(0x3a4658);
      beams[k].material.color.copy(col);
      beams[k].material.opacity = seen ? 0.35 + 0.55 * f : 0.18;
      beams[k].scale.set(1 + 2.6 * f, 1, 1 + 2.6 * f);
      zones[k].material.color.copy(col);
      zones[k].material.opacity = seen ? 0.02 + 0.20 * f : 0.012;
    }
    return updateField(paths || {});
  }

  window.CSI3D = { init, update, pairKey };
})();
