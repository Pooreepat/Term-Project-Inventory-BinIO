#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_inventory_data.py
สร้างข้อมูลจำลองสำหรับ Inventory-BinIO (categories.bin / items.bin / movements.bin)
- โครงสร้างไฟล์เหมือนโปรแกรมหลักทุกประการ (HEADER/INDEX/RECORD ฟอร์แมต identical)
- ใช้เฉพาะ Python Standard Library
"""
from __future__ import annotations
import os, sys, struct, argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# ----------------------------
# สเปกไฟล์/บันทัดฐาน (ต้องตรงกับโปรแกรมหลัก)
# ----------------------------
E = '<'                               # little-endian
HEADER_SIZE = 128
INDEX_SLOT_SIZE = 16
HEADER_FMT = E + '4s B B H I I I I I i I 92x'   # 128B
INDEX_FMT  = E + 'I I 8x'                        # 16B
TOMBSTONE_KEY = 0xFFFFFFFF

# Record format (ต้อง identical)
CAT_FMT  = E + 'B I 30s 80s 13x'; CAT_SIZE=128; CAT_PAD=115
ITEM_FMT = E + 'B I 30s I I I B 80x'; ITEM_SIZE=128; ITEM_PAD=48
MOVE_FMT = E + 'B I I I I I 30s 13x'; MOVE_SIZE=64;  MOVE_PAD=51

ITEM_STATUS = {0:'available', 1:'damaged', 2:'disposed'}
MOVE_TYPE   = {0:'issue', 1:'transfer', 2:'return', 3:'repair'}

def fit(s: str, n: int) -> bytes:
    return (s or '').encode('utf-8','ignore')[:n].ljust(n, b'\x00')

def ymd_to_int(s: str) -> int:
    if not s: return 0
    y,m,d = map(int, s.split('-'))
    return y*10000 + m*100 + d

# ----------------------------
# Header/Index โครงสร้าง
# ----------------------------
@dataclass
class Header:
    magic: bytes; version: int; endian: int; record_size: int
    created_at: int; updated_at: int; next_id: int
    active_count: int; deleted_count: int; free_head: int; index_slots: int
    def pack(self) -> bytes:
        return struct.pack(HEADER_FMT, self.magic, self.version, self.endian,
                           self.record_size, self.created_at, self.updated_at,
                           self.next_id, self.active_count, self.deleted_count,
                           self.free_head, self.index_slots)
    @classmethod
    def unpack(cls, b: bytes) -> 'Header':
        (magic, ver, ed, rsz, c_at, u_at, nid, ac, dc, fh, slots) = struct.unpack(HEADER_FMT, b)
        return cls(magic,ver,ed,rsz,c_at,u_at,nid,ac,dc,fh,slots)
    @classmethod
    def new(cls, magic: bytes, record_size: int, index_slots: int) -> 'Header':
        t = int(datetime.now().timestamp())
        return cls(magic, 1, 0, record_size, t, t, 1, 0, 0, -1, index_slots)

@dataclass
class IndexSlot:
    key: int; rec_index: int
    def pack(self) -> bytes: return struct.pack(INDEX_FMT, self.key, self.rec_index)
    @classmethod
    def unpack(cls, b: bytes) -> 'IndexSlot':
        k,ri = struct.unpack(INDEX_FMT, b); return cls(k,ri)

# ----------------------------
# ชั้นตารางไบนารี (ขั้นต่ำพอสำหรับ seeding)
# ----------------------------
class BinTable:
    def __init__(self, path: str, magic: bytes, rsize: int, rfmt: str, slots: int, pad_off: int):
        self.path=path; self.magic=magic; self.rsize=rsize; self.rfmt=rfmt
        self.slots=slots; self.pad_off=pad_off
        self.f=None; self.h: Optional[Header]=None

    def _lookup(self, key: int) -> Optional[int]:
        start = self._hash(key)
        for i in range(self.h.index_slots):
            j = (start + i) % self.h.index_slots
            sl = self._read_slot(j)
            if sl.key == 0:
                return None
            if sl.key == key:
                return sl.rec_index
        return None

    def read_record(self, key: int) -> Optional[bytes]:
        ri = self._lookup(key)
        if ri is None:
            return None
        self.f.seek(self._record_ofs(ri))
        return self.f.read(self.rsize)

    def update_record(self, key: int, packed: bytes) -> None:
        ri = self._lookup(key)
        if ri is None:
            raise KeyError('not found')
        self.f.seek(self._record_ofs(ri))
        self.f.write(packed)
        self._write_header()
        self._sync()


    def open_new(self):
        # สร้างไฟล์ใหม่เสมอ
        self.f = open(self.path, 'w+b')
        self.h = Header.new(self.magic, self.rsize, self.slots)
        self.f.seek(0); self.f.write(self.h.pack())
        for _ in range(self.slots):
            self.f.write(IndexSlot(0,0).pack())
        self._sync()

    def close(self):
        if self.f:
            self.f.flush(); os.fsync(self.f.fileno()); self.f.close(); self.f=None

    def _sync(self): self.f.flush(); os.fsync(self.f.fileno())
    def _write_header(self):
        self.h.updated_at = int(datetime.now().timestamp())
        self.f.seek(0); self.f.write(self.h.pack()); self._sync()

    def _index_ofs(self, slot: int) -> int: return HEADER_SIZE + slot*INDEX_SLOT_SIZE
    def _records_region_ofs(self) -> int: return HEADER_SIZE + self.h.index_slots*INDEX_SLOT_SIZE
    def _record_ofs(self, rec_index: int) -> int: return self._records_region_ofs() + rec_index*self.rsize
    def _records_count(self) -> int:
        self.f.seek(0, os.SEEK_END)
        payload = self.f.tell() - self._records_region_ofs()
        return 0 if payload <= 0 else payload // self.rsize
    def _read_slot(self, slot: int) -> IndexSlot:
        self.f.seek(self._index_ofs(slot)); return IndexSlot.unpack(self.f.read(INDEX_SLOT_SIZE))
    def _write_slot(self, slot: int, slotval: IndexSlot):
        self.f.seek(self._index_ofs(slot)); self.f.write(slotval.pack())

    def _hash(self, key: int) -> int: return key % self.h.index_slots
    def _find_slot_for_insert(self, key: int) -> int:
        start = self._hash(key); first_tomb = -1
        for i in range(self.h.index_slots):
            j = (start + i) % self.h.index_slots
            sl = self._read_slot(j)
            if sl.key == key:
                raise ValueError('duplicate key')
            if sl.key == TOMBSTONE_KEY and first_tomb < 0:
                first_tomb = j
            if sl.key == 0:
                return first_tomb if first_tomb >= 0 else j
        raise RuntimeError('index full')

    def next_id(self) -> int:
        nid = self.h.next_id; self.h.next_id += 1; self._write_header(); return nid

    def add_record(self, key: int, packed: bytes):
        i = self._records_count()
        # เขียนเรกคอร์ด
        self.f.seek(self._record_ofs(i)); self.f.write(packed)
        # เขียน index
        j = self._find_slot_for_insert(key); self._write_slot(j, IndexSlot(key, i))
        # header
        self.h.active_count += 1; self._write_header(); self._sync()

# ----------------------------
# ตารางเฉพาะ
# ----------------------------
class Categories(BinTable):
    def __init__(self, path: str, slots: int = 512):
        super().__init__(path, b'CATE', CAT_SIZE, CAT_FMT, slots, CAT_PAD)
    def pack(self, flag:int, cid:int, name:str, desc:str) -> bytes:
        return struct.pack(self.rfmt, flag, cid, fit(name,30), fit(desc,80))

class Items(BinTable):
    def __init__(self, path: str, slots: int = 4096):
        super().__init__(path, b'ITEM', ITEM_SIZE, ITEM_FMT, slots, ITEM_PAD)
    def pack(self, flag:int, iid:int, name:str, cat_id:int, qty:int, price_cents:int, status:int) -> bytes:
        return struct.pack(self.rfmt, flag, iid, fit(name,30), cat_id, qty, price_cents, status)

class Movements(BinTable):
    def __init__(self, path: str, slots: int = 8192):
        super().__init__(path, b'MOVE', MOVE_SIZE, MOVE_FMT, slots, MOVE_PAD)
    def pack(self, flag:int, mid:int, item_id:int, ymd:int, qty:int, typ:int, operator:str) -> bytes:
        return struct.pack(self.rfmt, flag, mid, item_id, ymd, qty, typ, fit(operator,30))

# ----------------------------
# Seed data
# ----------------------------
CATS = [
    ("Computers",   "PC, Laptop, Monitor, etc."),
    ("Furniture",   "Desks, Chairs, Cabinets"),
    ("Tools",       "Hand tools & power tools"),
    ("Appliances",  "Microwave, Fridge, etc."),
    ("Stationery",  "Paper, Pens, etc."),
    ("Networking",  "Switches, Routers, Cables"),
]

ITEMS = [
    # name,           cat_idx, qty,  price_thb, status
    ("Laptop Acer Aspire",   0,   10, 25000.00, 0),
    ("Desktop HP ProDesk",   0,    5, 32000.00, 0),
    ("Monitor 24\" IPS",     0,   12,  4500.00, 0),
    ("Office Chair Mesh",    1,   15,  4500.00, 0),
    ("Standing Desk 120cm",  1,    8, 12000.00, 0),
    ("Hammer 16oz",          2,   30,   200.00, 0),
    ("Electric Drill Bosch", 2,    6,  3500.00, 0),
    ("Microwave Sharp 20L",  3,    4,  2200.00, 0),
    ("A4 Copy Paper 80gsm",  4,   50,   120.00, 0),
    ("Gigabit Switch 16p",   5,    5,  2800.00, 0),
    ("Cat6 Cable 305m",      5,    2,  2900.00, 0),
    ("Old Dell OptiPlex",    0,    0,  1000.00, 2),  # disposed
    ("Broken Office Chair",  1,    1,  3000.00, 1),  # damaged
]

MOVES = [
    # (item_idx, ymd, qty, type, operator)
    (8,  "2025-09-01",  5, 0, "Somchai"),   # issue A4 paper 5
    (0,  "2025-09-02",  1, 0, "Anan"),      # issue laptop 1
    (0,  "2025-09-05",  1, 2, "Anan"),      # return laptop 1
    (9,  "2025-09-03",  1, 1, "Jiraporn"),  # transfer switch
    (6,  "2025-09-04",  1, 3, "Wipada"),    # repair drill
    (3,  "2025-09-06",  3, 0, "Nok"),       # issue chairs 3
    (3,  "2025-09-07",  1, 2, "Nok"),       # return chair 1
    (7,  "2025-09-08",  1, 0, "Korn"),      # issue microwave
    (10, "2025-09-09",  1, 1, "Preecha"),   # transfer Cat6 cable
    (5,  "2025-09-10", 10, 0, "Arthit"),    # issue hammer 10
]

# ----------------------------
# Seeder
# ----------------------------
def ensure_dir(p: str):
    if p and not os.path.isdir(p): os.makedirs(p, exist_ok=True)

def remove_if_exists(fp: str):
    try:
        if os.path.exists(fp): os.remove(fp)
    except Exception:
        pass

def seed(data_dir: str, reset: bool):
    ensure_dir(data_dir)
    paths = {
        'cats': os.path.join(data_dir, 'categories.bin'),
        'items': os.path.join(data_dir, 'items.bin'),
        'moves': os.path.join(data_dir, 'movements.bin'),
    }
    if reset:
        for p in paths.values(): remove_if_exists(p)

    # เปิดไฟล์ใหม่ (สร้าง header + index ว่าง)
    cats  = Categories(paths['cats']);  cats.open_new()
    items = Items(paths['items']);      items.open_new()
    moves = Movements(paths['moves']);  moves.open_new()

    try:
        # --- Seed categories ---
        cat_ids = []
        for name, desc in CATS:
            cid = cats.next_id()
            cats.add_record(cid, cats.pack(1, cid, name, desc))
            cat_ids.append(cid)

        # --- Seed items ---
        item_ids = []
        # (ใช้ mapping index -> real cat_id ตามลำดับที่เพิ่มจริง)
        for name, cat_idx, qty, price_thb, status in ITEMS:
            cat_id = cat_ids[cat_idx]
            iid = items.next_id()
            items.add_record(iid, items.pack(1, iid, name, cat_id, qty, int(round(price_thb*100)), status))
            item_ids.append(iid)

        # --- Seed movements + ปรับ qty ตาม type (issue/return) ---
        for item_idx, ymd_str, qty, type_code, operator in MOVES:
            iid = item_ids[item_idx]
            ymd = ymd_to_int(ymd_str)
            if qty <= 0: continue
            # อ่าน item ปัจจุบันเพื่อปรับ qty
            raw = items.read_record(iid)
            if not raw:
                continue
            f,iid0,nm,cat,qty0,prc,st = struct.unpack(ITEM_FMT, raw)  # unpack ตรง ๆ พอ
            new_qty = qty0
            if type_code == 0:   # issue
                if qty > qty0:   # ป้องกันติดลบ
                    qty = qty0
                new_qty -= qty
            elif type_code == 2: # return
                new_qty += qty
            # บันทึก movement
            mid = moves.next_id()
            moves.add_record(mid, moves.pack(1, mid, iid, ymd, qty, type_code, operator))
            # อัปเดต qty ถ้าจำเป็น
            if new_qty != qty0:
                items.update_record(iid, items.pack(1, iid, nm.decode('utf-8','ignore').rstrip('\x00'),
                                                    cat, new_qty, prc, st))

        print('* Seeding completed.')
        print('  -', paths['cats'])
        print('  -', paths['items'])
        print('  -', paths['moves'])
    finally:
        cats.close(); items.close(); moves.close()

# ----------------------------
# main
# ----------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Seed data for Inventory-BinIO')
    ap.add_argument('--data-dir', default='data_inv', help='โฟลเดอร์เก็บ .bin')
    ap.add_argument('--reset', action='store_true', help='ลบไฟล์เก่าและสร้างใหม่')
    args = ap.parse_args(argv)
    seed(args.data_dir, args.reset)
    return 0

if __name__ == '__main__':
    sys.exit(main())
