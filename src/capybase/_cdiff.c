/* capybase._cdiff — C-accelerated diff primitives.
 *
 * Exposes two functions used by capybase.diff:
 *
 *   char_ratio(a, b) -> float
 *       Character-level Gestalt similarity: 2*|LCS(a,b)| / (len(a)+len(b)).
 *       O(n*m) DP in C — fast even on multi-KB strings at thousands of calls.
 *       Replaces difflib.SequenceMatcher().ratio() on strings (SBCR fitness,
 *       entity-name similarity). Computes the TRUE maximal LCS, so it is
 *       correct where difflib's greedy matching undercounts.
 *
 *   histogram_match(list_a, list_b) -> list[(int, int)]
 *       The (i, j) index pairs of the maximal common subsequence under
 *       histogram-diff anchoring (rarest-element patience-LIS + recursive gap
 *       refinement). Operates on arbitrary Python lists (lines, tokens) using
 *       PyObject_RichCompareBool for equality. Mirrors capybase.diff's
 *       pure-Python _histogram_diff; this C path is the hot default.
 *
 * The module is optional: capybase.diff falls back to pure-Python when the
 * import fails (no compiler / failed build). No external C dependencies —
 * only the CPython API.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* -------------------------------------------------------------------------
 * char_ratio: 2*|LCS|/(len(a)+len(b)) over byte strings.
 *
 * Standard two-row DP. Memory: O(min(len(a), len(b))) via row swapping.
 * We always iterate over the longer string in the outer loop so the rows are
 * sized to the shorter one — keeps the DP table small on asymmetric inputs
 * (e.g. a short entity name vs a long side text).
 * ---------------------------------------------------------------------- */

static PyObject *
cdiff_char_ratio(PyObject *self, PyObject *args)
{
    const char *a, *b;
    Py_ssize_t len_a, len_b;

    if (!PyArg_ParseTuple(args, "s#s#", &a, &len_a, &b, &len_b)) {
        return NULL;
    }
    if (len_a == 0 && len_b == 0) {
        return PyFloat_FromDouble(1.0);
    }
    if (len_a == 0 || len_b == 0) {
        return PyFloat_FromDouble(0.0);
    }

    /* Outer loop over the longer string; rows sized to the shorter.
     * prev/curr are the DP rows over the shorter dimension (+1 for the base). */
    const char *outer = (len_a >= len_b) ? a : b;
    const char *inner = (len_a >= len_b) ? b : a;
    Py_ssize_t n_outer = (len_a >= len_b) ? len_a : len_b;
    Py_ssize_t n_inner = (len_a >= len_b) ? len_b : len_a;

    /* +1 column for the empty-base (index 0). Allocate in one block. */
    Py_ssize_t width = n_inner + 1;
    int *prev = (int *)PyMem_Malloc(sizeof(int) * (size_t)width);
    int *curr = (int *)PyMem_Malloc(sizeof(int) * (size_t)width);
    if (prev == NULL || curr == NULL) {
        PyMem_Free(prev);
        PyMem_Free(curr);
        PyErr_NoMemory();
        return NULL;
    }
    memset(prev, 0, sizeof(int) * (size_t)width);

    for (Py_ssize_t i = n_outer - 1; i >= 0; i--) {
        char oc = outer[i];
        curr[n_inner] = 0; /* base case: empty inner tail */
        for (Py_ssize_t j = n_inner - 1; j >= 0; j--) {
            if (oc == inner[j]) {
                curr[j] = prev[j + 1] + 1;
            } else {
                int down = prev[j];
                int right = curr[j + 1];
                curr[j] = (down >= right) ? down : right;
            }
        }
        /* Swap rows: prev becomes curr for the next outer iteration. */
        int *tmp = prev;
        prev = curr;
        curr = tmp;
    }
    /* After the swap, prev[0] holds LCS(outer[0:], inner[0:]). */
    int lcs = prev[0];
    PyMem_Free(prev);
    PyMem_Free(curr);

    double total = (double)(len_a + len_b);
    return PyFloat_FromDouble(2.0 * (double)lcs / total);
}

/* -------------------------------------------------------------------------
 * histogram_match: maximal common subsequence of two Python lists.
 *
 * Algorithm (mirrors capybase.diff._histogram_diff):
 *   1. For each a-index, find candidate b-indices (where a[i] == b[j]).
 *   2. Emit candidates in a-ascending order, b-descending within each a-index
 *      (the patience-diff trick: ensures one b per a-index survives the LIS).
 *   3. Longest strictly-increasing subsequence on b → the monotone matching.
 *   4. Recursive gap refinement: run the same on each unmatched gap sub-region.
 *
 * Equality is PyObject_RichCompareBool(==, Py_EQ) — works for any hashable.
 * Returns a Python list of (a_index, b_index) int tuples.
 * ---------------------------------------------------------------------- */

/* A candidate match: (a_index, b_index). */
typedef struct {
    Py_ssize_t a;
    Py_ssize_t b;
} Pair;

/* Patience LIS: longest strictly-increasing subsequence on the b-field.
 * Returns a malloc'd array of selected Pairs (caller frees) and sets *out_len.
 * Uses predecessor tracking + binary search over piles. */
static Pair *
patience_lis(const Pair *cands, Py_ssize_t n, Py_ssize_t *out_len)
{
    if (n == 0) {
        *out_len = 0;
        return NULL;
    }
    Py_ssize_t *piles = (Py_ssize_t *)PyMem_Malloc(sizeof(Py_ssize_t) * (size_t)n);
    Py_ssize_t *pred = (Py_ssize_t *)PyMem_Malloc(sizeof(Py_ssize_t) * (size_t)n);
    if (piles == NULL || pred == NULL) {
        PyMem_Free(piles);
        PyMem_Free(pred);
        *out_len = -1; /* signal OOM to caller */
        return NULL;
    }
    Py_ssize_t n_piles = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        /* Binary search: leftmost pile whose b >= cands[i].b (strictly increasing). */
        Py_ssize_t lo = 0, hi = n_piles;
        while (lo < hi) {
            Py_ssize_t mid = lo + (hi - lo) / 2;
            if (cands[piles[mid]].b < cands[i].b) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        pred[i] = (lo > 0) ? piles[lo - 1] : -1;
        if (lo == n_piles) {
            piles[n_piles++] = i;
        } else {
            piles[lo] = i;
        }
    }
    /* Reconstruct from the top pile. */
    Py_ssize_t result_len = n_piles;
    Pair *result = (Pair *)PyMem_Malloc(sizeof(Pair) * (size_t)result_len);
    if (result == NULL) {
        PyMem_Free(piles);
        PyMem_Free(pred);
        *out_len = -1;
        return NULL;
    }
    Py_ssize_t k = piles[n_piles - 1];
    for (Py_ssize_t idx = result_len - 1; idx >= 0; idx--) {
        result[idx] = cands[k];
        k = pred[k];
    }
    PyMem_Free(piles);
    PyMem_Free(pred);
    *out_len = result_len;
    return result;
}

/* Forward declarations: refine_gaps → refine_region → histogram_core → refine_gaps
 * (mutual recursion). Declared here so all three can reference each other
 * regardless of definition order below. */
static int refine_region(PyObject *list_a, PyObject *list_b,
                         Py_ssize_t a_lo, Py_ssize_t a_hi,
                         Py_ssize_t b_lo, Py_ssize_t b_hi,
                         Py_ssize_t off_a, Py_ssize_t off_b,
                         PyObject *out);
static int histogram_core(PyObject *list_a, PyObject *list_b,
                          Py_ssize_t a_lo, Py_ssize_t a_hi,
                          Py_ssize_t b_lo, Py_ssize_t b_hi,
                          Py_ssize_t off_a, Py_ssize_t off_b,
                          PyObject *out);

/* Recursive gap refinement: fill unmatched regions between `matches` with LCS
 * matches found by re-running the candidate+LIS on each gap sublist. Appends
 * to `out` (a Python list of (i,j) tuples). `offsets` add the sublist's
 * absolute position back. */
static int
refine_gaps(PyObject *list_a, PyObject *list_b,
            const Pair *matches, Py_ssize_t n_matches,
            Py_ssize_t off_a, Py_ssize_t off_b,
            PyObject *out)
{
    Py_ssize_t prev_a = 0, prev_b = 0;
    for (Py_ssize_t m = 0; m < n_matches; m++) {
        Py_ssize_t ai = matches[m].a, bj = matches[m].b;
        if (ai > prev_a && bj > prev_b) {
            /* Gap: list_a[prev_a:ai] vs list_b[prev_b:bj]. Recurse. */
            if (refine_region(list_a, list_b,
                              prev_a, ai, prev_b, bj,
                              off_a, off_b, out) < 0) {
                return -1;
            }
        }
        /* Emit the anchor pair (absolute indices). */
        PyObject *tup = Py_BuildValue("(nn)", (Py_ssize_t)(ai + off_a),
                                      (Py_ssize_t)(bj + off_b));
        if (tup == NULL) return -1;
        if (PyList_Append(out, tup) < 0) { Py_DECREF(tup); return -1; }
        Py_DECREF(tup);
        prev_a = ai + 1;
        prev_b = bj + 1;
    }
    return 0;
}

/* refine_region: re-run histogram diff on a gap sublist. The offsets carry the
 * sublist's absolute position so emitted pairs have global indices. */
static int
refine_region(PyObject *list_a, PyObject *list_b,
              Py_ssize_t a_lo, Py_ssize_t a_hi,
              Py_ssize_t b_lo, Py_ssize_t b_hi,
              Py_ssize_t off_a, Py_ssize_t off_b,
              PyObject *out)
{
    return histogram_core(list_a, list_b, a_lo, a_hi, b_lo, b_hi,
                          off_a, off_b, out);
}

/* The core: find the patience-LIS matching of list_a[a_lo:a_hi] vs
 * list_b[b_lo:b_hi], emit the pairs (absolute), then refine the gaps. */
static int
histogram_core(PyObject *list_a, PyObject *list_b,
               Py_ssize_t a_lo, Py_ssize_t a_hi,
               Py_ssize_t b_lo, Py_ssize_t b_hi,
               Py_ssize_t off_a, Py_ssize_t off_b,
               PyObject *out)
{
    Py_ssize_t na = a_hi - a_lo;
    Py_ssize_t nb = b_hi - b_lo;
    if (na <= 0 || nb <= 0) {
        return 0; /* empty region → nothing matches */
    }

    /* Build candidates: for each a-index, its b-indices (descending).
     * We scan b once per a-element via linear equality check. For typical
     * conflict-hunk sizes (5-50 lines) this is fine; the patience LIS is the
     * algorithmic work. */
    /* Upper bound on candidates: na * nb (every a vs every b). Allocate that. */
    Pair *cands = (Pair *)PyMem_Malloc(sizeof(Pair) * (size_t)na * (size_t)nb);
    if (cands == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t n_cands = 0;
    for (Py_ssize_t i = a_lo; i < a_hi; i++) {
        PyObject *ea = PyList_GET_ITEM(list_a, i);
        /* Collect b-indices for this a-element in DESCENDING order. */
        /* First pass: count; second pass: fill from the end. Cheaper to just
         * scan forward and collect ascending, then reverse-emit. For small nb
         * the simplicity wins. */
        for (Py_ssize_t j = b_hi - 1; j >= b_lo; j--) {
            PyObject *eb = PyList_GET_ITEM(list_b, j);
            int eq = PyObject_RichCompareBool(ea, eb, Py_EQ);
            if (eq < 0) { PyMem_Free(cands); return -1; }
            if (eq) {
                cands[n_cands].a = i - a_lo;
                cands[n_cands].b = j - b_lo;
                n_cands++;
            }
        }
    }

    if (n_cands == 0) {
        PyMem_Free(cands);
        return 0; /* no common element → pure replace */
    }

    /* Patience LIS over the candidates (sorted by a-ascending since we emitted
     * them in a-order; within an a-index, b is descending → one survives). */
    Py_ssize_t n_matches = 0;
    Pair *matches = patience_lis(cands, n_cands, &n_matches);
    PyMem_Free(cands);
    if (matches == NULL) {
        if (n_matches < 0) PyErr_NoMemory();
        return -1;
    }

    /* Refine gaps recursively (offsets carry the absolute positions). */
    int rc = refine_gaps(list_a, list_b, matches, n_matches,
                         a_lo + off_a, b_lo + off_b, out);
    PyMem_Free(matches);
    return rc;
}

static PyObject *
cdiff_histogram_match(PyObject *self, PyObject *args)
{
    PyObject *list_a, *list_b;
    if (!PyArg_ParseTuple(args, "OO", &list_a, &list_b)) {
        return NULL;
    }
    if (!PyList_Check(list_a) || !PyList_Check(list_b)) {
        PyErr_SetString(PyExc_TypeError,
                        "histogram_match requires two lists");
        return NULL;
    }
    Py_ssize_t na = PyList_GET_SIZE(list_a);
    Py_ssize_t nb = PyList_GET_SIZE(list_b);
    if (na == 0 || nb == 0) {
        return PyList_New(0); /* empty → no matches */
    }

    PyObject *out = PyList_New(0);
    if (out == NULL) return NULL;
    if (histogram_core(list_a, list_b, 0, na, 0, nb, 0, 0, out) < 0) {
        Py_DECREF(out);
        return NULL;
    }
    /* The recursive emit produces pairs in a per-branch order; sort by a-index
     * so the result is globally ascending (the caller expects sorted pairs). */
    if (PyList_Sort(out) < 0) {
        Py_DECREF(out);
        return NULL;
    }
    return out;
}

/* -------------------------------------------------------------------------
 * Module definition
 * ---------------------------------------------------------------------- */

static PyMethodDef _cdiff_methods[] = {
    {"char_ratio", cdiff_char_ratio, METH_VARARGS,
     "char_ratio(a, b) -> float\n\n"
     "Character-level Gestalt similarity: 2*|LCS|/(len(a)+len(b)).\n"
     "Returns 1.0 for identical, 0.0 for disjoint, in [0,1]."},
    {"histogram_match", cdiff_histogram_match, METH_VARARGS,
     "histogram_match(list_a, list_b) -> list[(int, int)]\n\n"
     "Maximal common subsequence as (a_index, b_index) pairs, sorted by\n"
     "a_index. Uses histogram-diff anchoring (rarest-element patience-LIS\n"
     "+ recursive gap refinement)."},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef _cdiff_module = {
    PyModuleDef_HEAD_INIT,
    "_cdiff",
    "C-accelerated diff primitives for capybase (char-level ratio, histogram diff).",
    -1,
    _cdiff_methods,
    NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit__cdiff(void)
{
    return PyModule_Create(&_cdiff_module);
}
