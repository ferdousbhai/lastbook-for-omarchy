// SPDX-License-Identifier: MIT
//! Mapbox Vector Tiles (https://github.com/mapbox/vector-tile-spec, v2): the protocol buffer decoded by hand, since the
//! format needs only varints, length-delimited fields and two fixed-width floats.

use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Str(String),
    Num(f64),
    Bool(bool),
}

impl Val {
    pub fn as_str(&self) -> Option<&str> {
        if let Val::Str(s) = self { Some(s) } else { None }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Val::Num(n) => Some(*n),
            Val::Bool(b) => Some(f64::from(u8::from(*b))),
            Val::Str(s) => s.parse().ok(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Geom {
    Point,
    Line,
    Polygon,
    Unknown,
}

#[derive(Clone, Debug)]
pub struct Feature {
    pub geom: Geom,
    pub props: HashMap<String, Val>,
    /// Rings or lines (points: one point each), in tile coordinates 0..extent.
    pub parts: Vec<Vec<(f32, f32)>>,
}

impl Feature {
    pub fn str(&self, k: &str) -> Option<&str> {
        self.props.get(k).and_then(Val::as_str)
    }
    pub fn num(&self, k: &str) -> Option<f64> {
        self.props.get(k).and_then(Val::as_f64)
    }
}

#[derive(Clone, Debug)]
pub struct Layer {
    pub name: String,
    pub extent: f32,
    pub features: Vec<Feature>,
}

struct Reader<'a> {
    b: &'a [u8],
    p: usize,
}

impl<'a> Reader<'a> {
    fn new(b: &'a [u8]) -> Self {
        Self { b, p: 0 }
    }
    fn done(&self) -> bool {
        self.p >= self.b.len()
    }
    fn varint(&mut self) -> Option<u64> {
        let mut out = 0u64;
        for shift in (0..64).step_by(7) {
            let byte = *self.b.get(self.p)?;
            self.p += 1;
            out |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Some(out);
            }
        }
        None
    }
    fn bytes(&mut self) -> Option<&'a [u8]> {
        let n = self.varint()? as usize;
        self.fixed(n)
    }
    fn fixed(&mut self, n: usize) -> Option<&'a [u8]> {
        let s = self.b.get(self.p..self.p + n)?;
        self.p += n;
        Some(s)
    }
    /// The next (field, wire type).
    fn key(&mut self) -> Option<(u64, u8)> {
        let k = self.varint()?;
        Some((k >> 3, (k & 7) as u8))
    }
    fn skip(&mut self, wire: u8) -> Option<()> {
        match wire {
            0 => {
                self.varint()?;
            }
            1 => {
                self.fixed(8)?;
            }
            2 => {
                self.bytes()?;
            }
            5 => {
                self.fixed(4)?;
            }
            _ => return None,
        }
        Some(())
    }
    fn packed(&mut self) -> Option<Vec<u64>> {
        let mut r = Reader::new(self.bytes()?);
        let mut out = Vec::new();
        while !r.done() {
            out.push(r.varint()?);
        }
        Some(out)
    }
}

fn zigzag(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

fn value(b: &[u8]) -> Option<Val> {
    let mut r = Reader::new(b);
    let mut v = None;
    while !r.done() {
        let (f, w) = r.key()?;
        v = Some(match (f, w) {
            (1, 2) => Val::Str(String::from_utf8_lossy(r.bytes()?).into_owned()),
            (2, 5) => Val::Num(f64::from(f32::from_le_bytes(r.fixed(4)?.try_into().ok()?))),
            (3, 1) => Val::Num(f64::from_le_bytes(r.fixed(8)?.try_into().ok()?)),
            (4, 0) => Val::Num(r.varint()? as i64 as f64),
            (5, 0) => Val::Num(r.varint()? as f64),
            (6, 0) => Val::Num(zigzag(r.varint()?) as f64),
            (7, 0) => Val::Bool(r.varint()? != 0),
            _ => {
                r.skip(w)?;
                continue;
            }
        });
    }
    v
}

fn geometry(cmds: &[u64]) -> Vec<Vec<(f32, f32)>> {
    let (mut parts, mut cur): (Vec<Vec<(f32, f32)>>, Vec<(f32, f32)>) = (Vec::new(), Vec::new());
    let (mut x, mut y) = (0i64, 0i64);
    let mut i = 0;
    while i < cmds.len() {
        let (cmd, count) = (cmds[i] & 7, (cmds[i] >> 3) as usize);
        i += 1;
        match cmd {
            1 | 2 => {
                for _ in 0..count {
                    if i + 1 >= cmds.len() {
                        break;
                    }
                    x += zigzag(cmds[i]);
                    y += zigzag(cmds[i + 1]);
                    i += 2;
                    if cmd == 1 && !cur.is_empty() {
                        parts.push(std::mem::take(&mut cur));
                    }
                    cur.push((x as f32, y as f32));
                }
            }
            7 => {
                if let Some(&first) = cur.first() {
                    cur.push(first);
                }
            }
            _ => break,
        }
    }
    if !cur.is_empty() {
        parts.push(cur);
    }
    parts
}

fn layer(b: &[u8]) -> Option<Layer> {
    let mut r = Reader::new(b);
    let (mut name, mut extent) = (String::new(), 4096.0f32);
    let (mut keys, mut vals, mut raw_features) = (Vec::new(), Vec::new(), Vec::new());
    while !r.done() {
        let (f, w) = r.key()?;
        match (f, w) {
            (1, 2) => name = String::from_utf8_lossy(r.bytes()?).into_owned(),
            (2, 2) => raw_features.push(r.bytes()?),
            (3, 2) => keys.push(String::from_utf8_lossy(r.bytes()?).into_owned()),
            (4, 2) => vals.push(value(r.bytes()?).unwrap_or(Val::Bool(false))),
            (5, 0) => extent = r.varint()? as f32,
            _ => r.skip(w)?,
        }
    }
    let mut features = Vec::with_capacity(raw_features.len());
    for fb in raw_features {
        let mut r = Reader::new(fb);
        let (mut tags, mut geom, mut kind) = (Vec::new(), Vec::new(), Geom::Unknown);
        while !r.done() {
            let (f, w) = r.key()?;
            match (f, w) {
                (2, 2) => tags = r.packed()?,
                (3, 0) => {
                    kind = match r.varint()? {
                        1 => Geom::Point,
                        2 => Geom::Line,
                        3 => Geom::Polygon,
                        _ => Geom::Unknown,
                    }
                }
                (4, 2) => geom = r.packed()?,
                _ => r.skip(w)?,
            }
        }
        let mut props = HashMap::new();
        for pair in tags.chunks_exact(2) {
            if let (Some(k), Some(v)) = (keys.get(pair[0] as usize), vals.get(pair[1] as usize)) {
                props.insert(k.clone(), v.clone());
            }
        }
        features.push(Feature { geom: kind, props, parts: geometry(&geom) });
    }
    Some(Layer { name, extent, features })
}

/// Decodes a tile (gzip-compressed or not).
pub fn decode(data: &[u8]) -> Vec<Layer> {
    let raw;
    let data = if data.starts_with(&[0x1f, 0x8b]) {
        let mut out = Vec::new();
        use std::io::Read;
        if flate2::read::GzDecoder::new(data).read_to_end(&mut out).is_err() {
            return Vec::new();
        }
        raw = out;
        &raw[..]
    } else {
        data
    };
    let mut r = Reader::new(data);
    let mut layers = Vec::new();
    while !r.done() {
        let Some((f, w)) = r.key() else { break };
        if f == 3 && w == 2 {
            if let Some(b) = r.bytes()
                && let Some(l) = layer(b)
            {
                layers.push(l);
            }
        } else if r.skip(w).is_none() {
            break;
        }
    }
    layers
}

#[cfg(test)]
mod tests {
    use super::*;

    fn varint(mut n: u64, out: &mut Vec<u8>) {
        loop {
            let b = (n & 0x7f) as u8;
            n >>= 7;
            if n == 0 {
                out.push(b);
                break;
            }
            out.push(b | 0x80);
        }
    }
    fn field(f: u64, bytes: &[u8], out: &mut Vec<u8>) {
        varint(f << 3 | 2, out);
        varint(bytes.len() as u64, out);
        out.extend_from_slice(bytes);
    }
    fn zz(n: i64) -> u64 {
        ((n << 1) ^ (n >> 63)) as u64
    }

    #[test]
    fn decodes_a_square_with_tags() {
        // One layer "water" with one polygon (0,0)-(10,0)-(10,10) closed, tagged kind=ocean.
        let mut geom = Vec::new();
        for v in [9, zz(0), zz(0), 2 << 3 | 2, zz(10), zz(0), zz(0), zz(10), 7 | 1 << 3] {
            varint(v, &mut geom);
        }
        let mut feat = Vec::new();
        let mut tags = Vec::new();
        varint(0, &mut tags);
        varint(0, &mut tags);
        field(2, &tags, &mut feat);
        varint(3 << 3, &mut feat);
        varint(3, &mut feat);
        field(4, &geom, &mut feat);
        let mut val = Vec::new();
        field(1, b"ocean", &mut val);
        let mut lay = Vec::new();
        field(1, b"water", &mut lay);
        field(2, &feat, &mut lay);
        field(3, b"kind", &mut lay);
        field(4, &val, &mut lay);
        varint(5 << 3, &mut lay);
        varint(4096, &mut lay);
        let mut tile = Vec::new();
        field(3, &lay, &mut tile);
        let layers = decode(&tile);
        assert_eq!(layers.len(), 1);
        assert_eq!((layers[0].name.as_str(), layers[0].extent), ("water", 4096.0));
        let f = &layers[0].features[0];
        assert_eq!(f.geom, Geom::Polygon);
        assert_eq!(f.str("kind"), Some("ocean"));
        assert_eq!(f.parts, vec![vec![(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)]]);
    }
}
