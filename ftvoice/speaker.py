"""Owner-only gating: is this speech segment the enrolled user?

Two pretrained speaker-embedding networks (NeMo TitaNet-small and 3D-Speaker ERes2Net) turn a
segment into voice fingerprints. On top of them we keep a per-user model:
  * every enrollment clip's embedding (score = max(centroid cosine, mean of top-3 clip cosines))
  * an impostor cohort (TV dialogue / other voices heard in the room, plus synthetic
    voices) used for AS-norm score normalisation
  * a small logistic-regression head trained on owner-vs-impostor features, which is the
    "module trained on my voice". It is retrained by `ftv train` and as the cohort grows.
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx

log = logging.getLogger("speaker")

# Picked by A/B test (titanet + eres2net separated voices best; CAM++/ResNet34 confused similar voices).
MODELS = {
    "titanet": "nemo_en_titanet_small.onnx",
    "eres2net": "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
}
PRIMARY = "titanet"
SR = 16000
COHORT_MAX = 3000
TOPK = 200


def _unit(x):
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


class SpeakerGate:
    def __init__(self, model_dir, profile_path, threads=2):
        self.profile_path = profile_path
        self.extractors = {}
        for k, f in MODELS.items():
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=os.path.join(model_dir, f), num_threads=threads)
            self.extractors[k] = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(len(MODELS))
        self.enroll = {k: np.zeros((0, 0), np.float32) for k in MODELS}
        self.cohort = {k: np.zeros((0, 0), np.float32) for k in MODELS}
        self.head = None          # {"w": [...], "b": float, "mu": [...], "sd": [...]}
        self.threshold = 0.5      # on head probability
        self.cos_threshold = 0.6  # floor on mean raw cosine; always enforced
        self.load()

    # ---------- embeddings ----------
    def _embed1(self, k, samples):
        ex = self.extractors[k]
        s = ex.create_stream()
        s.accept_waveform(SR, samples)
        s.input_finished()
        return _unit(ex.compute(s))

    def embed(self, samples):
        # both networks in parallel (onnxruntime releases the GIL)
        futs = {k: self.pool.submit(self._embed1, k, samples) for k in self.extractors}
        return {k: f.result() for k, f in futs.items()}

    # ---------- persistence ----------
    @property
    def enrolled(self):
        return self.enroll[PRIMARY].shape[0] >= 3

    def load(self):
        if not os.path.exists(self.profile_path):
            return
        d = np.load(self.profile_path, allow_pickle=False)
        for k in MODELS:
            if f"enroll_{k}" in d:
                self.enroll[k] = d[f"enroll_{k}"]
            if f"cohort_{k}" in d:
                self.cohort[k] = d[f"cohort_{k}"]
        meta = json.loads(str(d["meta"])) if "meta" in d else {}
        self.head = meta.get("head")
        self.threshold = meta.get("threshold", 0.5)
        self.cos_threshold = meta.get("cos_threshold", 0.6)
        log.info("speaker profile: %d enroll, %d cohort, head=%s thr=%.2f",
                 self.enroll[PRIMARY].shape[0], self.cohort[PRIMARY].shape[0], bool(self.head), self.threshold)

    def save(self, extra_meta=None):
        meta = {"head": self.head, "threshold": self.threshold, "cos_threshold": self.cos_threshold, "saved": time.time(), **(extra_meta or {})}
        arrs = {f"enroll_{k}": v for k, v in self.enroll.items()}
        arrs.update({f"cohort_{k}": v for k, v in self.cohort.items()})
        tmp = self.profile_path + ".tmp.npz"
        np.savez(tmp, meta=json.dumps(meta), **arrs)
        os.replace(tmp, self.profile_path)

    # ---------- scoring ----------
    def _raw(self, k, e, enroll=None):
        E = self.enroll[k] if enroll is None else enroll
        sims = E @ e
        cent = _unit(E.mean(0)) @ e
        top = np.sort(sims)[-3:].mean()
        return float(max(cent, top)), E

    def _asnorm(self, k, e, raw, E):
        C = self.cohort[k]
        if C.shape[0] < 20:
            return raw
        cs = np.sort(C @ e)[-TOPK:]                       # test vs cohort
        es = np.sort(C @ _unit(E.mean(0)))[-TOPK:]        # enroll centroid vs cohort
        return float(0.5 * ((raw - cs.mean()) / (cs.std() + 1e-6) + (raw - es.mean()) / (es.std() + 1e-6)))

    def features(self, emb, dur, enroll=None):
        f = []
        for k in MODELS:
            raw, E = self._raw(k, emb[k], None if enroll is None else enroll[k])
            f += [raw, self._asnorm(k, emb[k], raw, E)]
        f.append(float(np.log(max(dur, 0.1))))
        return np.array(f, np.float32)

    def _prob(self, f):
        h = self.head
        z = (f - np.array(h["mu"])) / np.array(h["sd"])
        return float(1 / (1 + np.exp(-(z @ np.array(h["w"]) + h["b"]))))

    def score(self, samples, emb=None):
        """Returns (accepted, score, info, emb)."""
        dur = len(samples) / SR
        emb = emb or self.embed(samples)
        with self.lock:
            if not self.enrolled:
                return True, 1.0, {"reason": "not enrolled"}, emb
            f = self.features(emb, dur)
            cos = float((f[0] + f[2]) / 2)     # mean raw cosine of the two models
            # very short clips carry little voice information: demand a bit more
            short = dur < 0.8
            cthr = self.cos_threshold + (0.03 if short else 0.0)
            if self.head:
                s, thr = self._prob(f), min(0.97, self.threshold + (0.05 if short else 0.0))
                ok = s >= thr and cos >= cthr     # the trained head AND the similarity floor must agree
            else:
                s, thr = cos, cthr
                ok = cos >= cthr
            return ok, s, {"cos": round(cos, 3), "cthr": round(cthr, 3), "thr": round(thr, 3), "dur": round(dur, 2)}, emb

    # ---------- learning ----------
    def add_enroll(self, embs):
        with self.lock:
            for k in MODELS:
                new = np.stack([e[k] for e in embs])
                self.enroll[k] = new if self.enroll[k].size == 0 else np.vstack([self.enroll[k], new])

    def add_cohort(self, emb):
        with self.lock:
            for k in MODELS:
                c = self.cohort[k]
                self.cohort[k] = emb[k][None] if c.size == 0 else np.vstack([c, emb[k][None]])[-COHORT_MAX:]

    def train(self, neg_durs=None):
        self.cosine_threshold_from_enroll()
        return self._train()

    def _train(self):
        """Fit the per-user logistic-regression head with leave-one-out owner features vs cohort features."""
        with self.lock:
            n = self.enroll[PRIMARY].shape[0]
            m = self.cohort[PRIMARY].shape[0]
            if n < 5 or m < 20:
                self.head = None
                # cosine-only fallback threshold: just below the owner's leave-one-out scores
                return {"ok": False, "reason": f"need >=5 enroll and >=20 cohort (have {n}, {m}); using cosine threshold"}
            pos = []
            for i in range(n):
                keep = [j for j in range(n) if j != i]
                emb = {k: self.enroll[k][i] for k in MODELS}
                pos.append(self.features(emb, 1.5, {k: self.enroll[k][keep] for k in MODELS}))
            rng = np.random.default_rng(0)
            idx = rng.choice(m, size=min(m, 1500), replace=False)
            neg = [self.features({k: self.cohort[k][i] for k in MODELS}, float(rng.uniform(0.6, 3.0))) for i in idx]
            X = np.vstack([pos, neg]).astype(np.float64)
            y = np.r_[np.ones(len(pos)), np.zeros(len(neg))]
            mu, sd = X.mean(0), X.std(0) + 1e-6
            Z = (X - mu) / sd
            w, b = np.zeros(Z.shape[1]), 0.0
            cw = np.where(y == 1, 0.5 / y.mean(), 0.5 / (1 - y.mean()))   # balance classes
            for _ in range(3000):
                p = 1 / (1 + np.exp(-(Z @ w + b)))
                g = (p - y) * cw
                w -= 0.1 * (Z.T @ g / len(y) + 1e-3 * w)
                b -= 0.1 * g.mean()
            self.head = {"w": w.tolist(), "b": float(b), "mu": mu.tolist(), "sd": sd.tolist()}
            pp = np.array([self._prob(f) for f in pos])
            pn = np.array([self._prob(f) for f in neg])
            # threshold: keep ~all of the owner's held-out clips, but never accept the top 0.5% of impostors
            thr = float(min(np.percentile(pp, 5), 0.9))
            thr = max(thr, float(np.percentile(pn, 99.5)) + 1e-3, 0.3)
            self.threshold = round(min(thr, 0.95), 3)
            stats = {"ok": True, "owner_clips": n, "cohort": m, "threshold": self.threshold,
                     "owner_min": round(float(pp.min()), 3), "owner_median": round(float(np.median(pp)), 3),
                     "impostor_max": round(float(pn.max()), 3), "impostor_p99": round(float(np.percentile(pn, 99)), 3),
                     "owner_accept_rate": round(float((pp >= self.threshold).mean()), 3),
                     "impostor_accept_rate": round(float((pn >= self.threshold).mean()), 4)}
            log.info("trained head: %s", stats)
            return stats

    def cosine_threshold_from_enroll(self):
        """Without a head, set the fused-cosine threshold from leave-one-out owner scores."""
        with self.lock:
            n = self.enroll[PRIMARY].shape[0]
            if n < 3:
                return None
            sc = []
            for i in range(n):
                keep = [j for j in range(n) if j != i]
                emb = {k: self.enroll[k][i] for k in MODELS}
                f = self.features(emb, 1.5, {k: self.enroll[k][keep] for k in MODELS})
                sc.append((f[0] + f[2]) / 2)
            self.cos_threshold = round(float(min(0.8, max(0.6, np.percentile(sc, 5) - 0.08))), 3)
            # if impostor cohort exists, keep the floor above the bulk of it
            if self.cohort[PRIMARY].shape[0] >= 20:
                imp = [float((self._raw(k, self.cohort[k][i])[0])) for i in range(min(500, self.cohort[PRIMARY].shape[0])) for k in [PRIMARY]]
                self.cos_threshold = round(max(self.cos_threshold, min(float(np.percentile(imp, 99)) + 0.02, float(np.percentile(sc, 5)) - 0.02)), 3)
            return self.cos_threshold
