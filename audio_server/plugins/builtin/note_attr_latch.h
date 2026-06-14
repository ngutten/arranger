#pragma once
// note_attr_latch.h — Per-note attribute latching, shared by synthesizers.
//
// Note-attributes are named float values carried by NoteAttr events that are
// dispatched immediately *before* the NoteOn for the same (channel,pitch) at
// the same beat (the scheduler sorts NoteAttr ahead of NoteOn).  A consumer
// stashes incoming attrs in a PendingAttrStore and, at note_on time, drains
// them into the voice that is being triggered.
//
// Composition semantics are applied by the consumer, not here:
//   - continuous attrs (e.g. "attack")    : multiply the param (neutral = 1.0)
//   - categorical attrs (e.g. "excitation"): override the param if present
//
// Header-only and allocation-free: safe to touch from the audio thread.

#include <cstring>
#include <string>

// ---------------------------------------------------------------------------
// AttrRemap — re-target incoming note-attr ids onto the slot a synth consumes.
// ---------------------------------------------------------------------------
// Configured from a "src:dst,src2:dst2" string (e.g. "swell:attack").  apply()
// is allocation-free (audio-thread safe); parse() runs on the main thread.

struct AttrRemap {
    static constexpr int MAX = 8;
    static constexpr int LEN = 20;
    char src_[MAX][LEN];
    char dst_[MAX][LEN];
    int  count = 0;

    void clear() { count = 0; }

    static void copy_trim(char* out, const std::string& s) {
        size_t b = s.find_first_not_of(" \t");
        size_t e = s.find_last_not_of(" \t");
        std::string t = (b == std::string::npos) ? "" : s.substr(b, e - b + 1);
        std::strncpy(out, t.c_str(), LEN - 1);
        out[LEN - 1] = '\0';
    }

    void parse(const std::string& spec) {
        clear();
        size_t i = 0;
        while (i <= spec.size() && count < MAX) {
            size_t comma = spec.find(',', i);
            std::string pair = spec.substr(i, comma == std::string::npos
                                              ? std::string::npos : comma - i);
            size_t colon = pair.find(':');
            if (colon != std::string::npos) {
                copy_trim(src_[count], pair.substr(0, colon));
                copy_trim(dst_[count], pair.substr(colon + 1));
                if (src_[count][0] && dst_[count][0]) ++count;
            }
            if (comma == std::string::npos) break;
            i = comma + 1;
        }
    }

    // Returns the remapped id, or the original if no rule matches.
    const char* apply(const char* id) const {
        for (int k = 0; k < count; ++k)
            if (std::strncmp(src_[k], id, LEN) == 0) return dst_[k];
        return id;
    }
};

// ---------------------------------------------------------------------------
// NoteAttrSet — a small bag of named float attributes for one note.
// ---------------------------------------------------------------------------

struct NoteAttrSet {
    static constexpr int MAX = 6;
    struct Entry { char id[20]; float value; };
    Entry entries[MAX];
    int   count = 0;

    void clear() { count = 0; }

    void set(const char* id, float v) {
        for (int i = 0; i < count; ++i)
            if (std::strncmp(entries[i].id, id, sizeof(entries[i].id)) == 0) {
                entries[i].value = v;
                return;
            }
        if (count < MAX) {
            std::strncpy(entries[count].id, id, sizeof(entries[count].id) - 1);
            entries[count].id[sizeof(entries[count].id) - 1] = '\0';
            entries[count].value = v;
            ++count;
        }
    }

    // Returns pointer to the latched value, or nullptr if the attr is absent.
    const float* get(const char* id) const {
        for (int i = 0; i < count; ++i)
            if (std::strncmp(entries[i].id, id, sizeof(entries[i].id)) == 0)
                return &entries[i].value;
        return nullptr;
    }

    float get_or(const char* id, float dflt) const {
        const float* p = get(id);
        return p ? *p : dflt;
    }
};

// ---------------------------------------------------------------------------
// PendingAttrStore — attrs that have arrived but whose NoteOn has not fired.
// ---------------------------------------------------------------------------
// Keyed by (channel,pitch).  Fixed capacity, no heap — RT-safe.  Stale slots
// (an attr whose NoteOn never arrives) are reclaimed as new ones come in.

struct PendingAttrStore {
    static constexpr int SLOTS = 32;
    struct Slot { int key = -1; NoteAttrSet set; };
    Slot slots[SLOTS];

    static int make_key(int ch, int pitch) {
        return ((ch & 0xFF) << 8) | (pitch & 0xFF);
    }

    void set(int ch, int pitch, const char* id, float v) {
        int key = make_key(ch, pitch);
        Slot* freeSlot = nullptr;
        for (auto& s : slots) {
            if (s.key == key) { s.set.set(id, v); return; }
            if (!freeSlot && s.key == -1) freeSlot = &s;
        }
        if (freeSlot) {
            freeSlot->key = key;
            freeSlot->set.clear();
            freeSlot->set.set(id, v);
        }
        // If the table is full we drop the attr rather than evict — bounded and
        // benign: a note simply renders with its default parameters.
    }

    // Move latched attrs for (ch,pitch) into `out`, freeing the slot.  When no
    // attrs were pending, `out` is cleared (neutral).
    void take(int ch, int pitch, NoteAttrSet& out) {
        int key = make_key(ch, pitch);
        for (auto& s : slots) {
            if (s.key == key) { out = s.set; s.key = -1; return; }
        }
        out.clear();
    }

    void clear() { for (auto& s : slots) s.key = -1; }
};
