"""Simulation Repository: pre-stored simulation results, grouped into *sets*.

Directory layout (root is configurable):

    <root>/
      typical/            -> set "Typical"
        3.4GHz.csv        -> one Dataset (FREQ from content or file name)
        3.5GHz.csv
      corner_ss/          -> set "Corner SS"
        sweep.csv         -> long format with a FREQ column -> split per FREQ
      previous.xlsx       -> set "Previous" (one Dataset per sheet)
      new_design.csv      -> set "New Design"

Every file goes through the same Sheet Parser as measurement data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.models import Dataset, Level, Message, Source
from excel.reader import ReaderError, read_csv, read_workbook
from excel.sheet_parser import parse_sheet
from excel.splitter import split_by_frequency, split_side_by_side
from simulation.matching import match_by_frequency
from utils.freq import format_freq

SUPPORTED = (".txt", ".dat", ".tsv", ".csv", ".xlsx", ".xlsm")
TEXT_EXT = (".txt", ".dat", ".tsv", ".csv")


def pretty_set_name(stem: str) -> str:
    words = stem.replace("-", " ").replace("_", " ").split()
    return " ".join(w.upper() if len(w) <= 2 else w[:1].upper() + w[1:] for w in words) or stem


@dataclass
class SimulationSet:
    name: str
    path: str
    datasets: List[Dataset] = field(default_factory=list)

    def frequencies(self) -> List[float]:
        return sorted(d.freq_hz for d in self.datasets if d.freq_hz is not None)

    def find(self, freq_hz: Optional[float], tol_hz: float = 1e6) -> Optional[Dataset]:
        return match_by_frequency(freq_hz, self.datasets, tol_hz)


@dataclass
class SimulationRepository:
    root: str = ""
    sets: Dict[str, SimulationSet] = field(default_factory=dict)
    messages: List[Message] = field(default_factory=list)

    keep_order: bool = False

    @property
    def set_names(self) -> List[str]:
        if self.keep_order:
            return list(self.sets)
        # "Typical" first if present, then alphabetical
        names = sorted(self.sets)
        if "Typical" in names:
            names.remove("Typical")
            names.insert(0, "Typical")
        return names

    def get(self, name: str) -> Optional[SimulationSet]:
        return self.sets.get(name)


def _datasets_from_file(path: str, set_name: str, msgs: List[Message]) -> List[Dataset]:
    ext = os.path.splitext(path)[1].lower()
    try:
        raws = [read_csv(path)] if ext in TEXT_EXT else read_workbook(path)
    except ReaderError as exc:
        msgs.append(Message(Level.WARNING, f"Simulation 파일 건너뜀 ({os.path.basename(path)}): {exc}"))
        return []
    out: List[Dataset] = []
    stem = os.path.splitext(os.path.basename(path))[0]
    for raw in raws:
        parts = [p for block in split_side_by_side(raw) for p in split_by_frequency(block)]
        for part in parts:
            res = parse_sheet(part, Source.SIMULATION, fallback_name=stem)
            if not res.ok:
                errs = "; ".join(m.text for m in res.errors()) or "unknown"
                msgs.append(Message(Level.WARNING,
                                    f"Simulation '{os.path.basename(path)}::{part.name}' 무시: {errs}"))
                continue
            ds = res.dataset
            ds.group = set_name
            dropped = drop_power_foldback(ds)
            if dropped:
                msgs.append(Message(Level.INFO, f"Simulation '{os.path.basename(path)}' {format_freq(ds.freq_hz)}: "
                                                f"Pout가 이전보다 낮아진 {dropped}개 점 제외 (포화 이후)"))
            if ds.freq_hz is None:
                msgs.append(Message(Level.WARNING,
                                    f"Simulation '{os.path.basename(path)}::{part.name}' FREQ 인식 실패 — "
                                    f"파일명에 FREQ를 넣거나(예: 3.6GHz.csv) FREQ column을 추가해주세요."))
            ds.name = f"{set_name} @ {format_freq(ds.freq_hz)}"
            out.append(ds)
    return out


def drop_power_foldback(ds: Dataset) -> int:
    """Saturation fold-back: once the amplifier cannot push more power, the sweep
    gives Pout values LOWER than an earlier point. Those rows are removed (in file
    order); the earlier, lower-power points stay valid. Returns rows removed."""
    import numpy as np
    pout = ds.columns.get("pout")
    if pout is None or len(pout) < 2:
        return 0
    keep = np.ones(len(pout), dtype=bool)
    best = -np.inf
    for i, v in enumerate(pout):
        if not np.isfinite(v):
            continue
        if v < best:
            keep[i] = False
        else:
            best = v
    n = int((~keep).sum())
    if n:
        ds.columns = {k: np.asarray(a)[keep] if len(a) == len(keep) else a for k, a in ds.columns.items()}
    return n


def load_simulation_repository(root: str) -> SimulationRepository:
    repo = SimulationRepository(root=root)
    if not root or not os.path.isdir(root):
        repo.messages.append(Message(Level.WARNING, f"Simulation 폴더를 찾을 수 없습니다: {root}"))
        return repo

    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if entry.startswith((".", "~$")):
            continue
        if os.path.isdir(full):
            name = pretty_set_name(entry)
            sset = SimulationSet(name, full)
            for fn in sorted(os.listdir(full)):
                if fn.lower().endswith(SUPPORTED) and not fn.startswith("~$"):
                    sset.datasets += _datasets_from_file(os.path.join(full, fn), name, repo.messages)
        elif entry.lower().endswith(SUPPORTED):
            name = pretty_set_name(os.path.splitext(entry)[0])
            sset = SimulationSet(name, full, _datasets_from_file(full, name, repo.messages))
        else:
            continue
        if sset.datasets:
            if name in repo.sets:
                repo.sets[name].datasets += sset.datasets
            else:
                repo.sets[name] = sset

    n = sum(len(s.datasets) for s in repo.sets.values())
    repo.messages.append(Message(Level.INFO, f"Simulation: {len(repo.sets)}개 Set, {n}개 Dataset 로드"))
    return repo


def load_simulation_files(paths) -> SimulationRepository:
    """User-chosen simulation files (e.g. 3B.txt, 3X.txt, 3P.txt, 3K.txt): one set per file,
    named after the file. A FREQ column with several values is split per frequency."""
    paths = [p for p in paths if p]
    root = os.path.dirname(paths[0]) if paths else ""
    repo = SimulationRepository(root=root, keep_order=True)
    for p in paths:
        if not os.path.isfile(p):
            repo.messages.append(Message(Level.WARNING, f"Simulation 파일을 찾을 수 없습니다: {p}"))
            continue
        name = pretty_set_name(os.path.splitext(os.path.basename(p))[0])
        sset = SimulationSet(name, p, _datasets_from_file(p, name, repo.messages))
        if sset.datasets:
            if name in repo.sets:
                repo.sets[name].datasets += sset.datasets
            else:
                repo.sets[name] = sset
        else:
            repo.messages.append(Message(Level.WARNING, f"{os.path.basename(p)}: 읽을 수 있는 데이터가 없습니다 "
                                                          f"(헤더: 주파수/Freq, Pout, PAE, Gain)."))
    n = sum(len(s.datasets) for s in repo.sets.values())
    repo.messages.append(Message(Level.INFO, f"Simulation 파일 {len(paths)}개: {len(repo.sets)}개 Set, {n}개 Dataset"))
    return repo
