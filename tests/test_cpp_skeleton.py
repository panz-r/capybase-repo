"""Tests for the C++ skeleton extractor.

Mirrors the C skeleton test suite's coverage: each entity kind, masking
robustness, safe degradation, render dedup/truncation. Adds C++-specific
coverage: namespace transparency, class-body method descent, template prefix
skipping, operator overloads, using declarations.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.adapters.cpp_skeleton import extract_skeleton  # noqa: E402


# ---------------------------------------------------------------------------
# Namespace transparency
# ---------------------------------------------------------------------------

def test_namespace_functions_surface():
    """Functions inside a namespace are captured (the namespace brace is
    transparent — declarations surface at depth 0)."""
    src = """\
namespace mylib {
int foo(int x);
void bar(void);
}
"""
    sk = extract_skeleton(src)
    names = {f.split("(", 1)[0] for f in sk.functions}
    assert "foo" in names, f"foo missing; got: {names}"
    assert "bar" in names, f"bar missing; got: {names}"


def test_nested_namespace():
    """Nested namespaces are transparent at all levels."""
    src = """\
namespace outer {
namespace inner {
int deep_func(int x);
}
}
"""
    sk = extract_skeleton(src)
    names = {f.split("(", 1)[0] for f in sk.functions}
    assert "deep_func" in names


def test_function_after_namespace():
    """A function definition after a namespace block is still captured (the
    namespace brace skip must not desynchronize the depth tracker)."""
    src = """\
namespace ns {
int inside(void);
}
int after_ns(int y) { return y; }
"""
    sk = extract_skeleton(src)
    names = {f.split("(", 1)[0] for f in sk.functions}
    assert "inside" in names
    assert "after_ns" in names


# ---------------------------------------------------------------------------
# Class body descent + method extraction
# ---------------------------------------------------------------------------

def test_class_name_and_methods():
    """A class definition records the class name AND its method signatures as
    ClassName::MethodName(params)."""
    src = """\
class Widget {
public:
    Widget(int x);
    int getSize() const;
    void resize(int w, int h);
private:
    int width_;
};
"""
    sk = extract_skeleton(src)
    assert "Widget" in sk.structs
    # Methods are qualified with ClassName::.
    sigs = set(sk.functions)
    assert any(s.startswith("Widget::Widget(") for s in sigs), f"ctor missing: {sigs}"
    assert any(s.startswith("Widget::getSize(") for s in sigs), f"getSize missing: {sigs}"
    assert any(s.startswith("Widget::resize(") for s in sigs), f"resize missing: {sigs}"


def test_struct_method_extraction():
    """C++ structs can have methods too."""
    src = """\
struct Point {
    int x, y;
    double distance() const;
    void move(int dx, int dy);
};
"""
    sk = extract_skeleton(src)
    assert "Point" in sk.structs
    assert any("Point::distance(" in f for f in sk.functions)
    assert any("Point::move(" in f for f in sk.functions)


def test_class_inside_namespace():
    """A class inside a namespace: both the class name and its methods are
    captured (namespace transparency + class body descent compose)."""
    src = """\
namespace ui {
class Button {
public:
    void click();
    void render(int x, int y);
};
}
"""
    sk = extract_skeleton(src)
    assert "Button" in sk.structs
    assert any("Button::click(" in f for f in sk.functions)
    assert any("Button::render(" in f for f in sk.functions)


def test_class_fields_skipped():
    """Fields (Type name;) inside a class are NOT captured as functions —
    only methods (which have parens) are."""
    src = """\
class Config {
    int timeout;
    bool verbose;
    void setVerbose(bool v);
};
"""
    sk = extract_skeleton(src)
    # setVerbose is a method; timeout/verbose are fields.
    assert any("Config::setVerbose(" in f for f in sk.functions)
    field_names = {f.split("(", 1)[0] for f in sk.functions}
    assert "timeout" not in field_names
    assert "verbose" not in field_names


# ---------------------------------------------------------------------------
# Template handling
# ---------------------------------------------------------------------------

def test_template_function():
    """template<typename T> prefix is skipped; the function beneath is captured."""
    src = """\
template<typename T>
T maxValue(T a, T b);

template<typename K, typename V>
V lookup(K key);
"""
    sk = extract_skeleton(src)
    names = {f.split("(", 1)[0] for f in sk.functions}
    assert "maxValue" in names
    assert "lookup" in names


def test_template_class():
    """template<typename T> class Foo { ... } — template prefix skipped, class
    name + methods captured."""
    src = """\
template<typename T>
class Container {
public:
    void add(T item);
    T get(int index) const;
};
"""
    sk = extract_skeleton(src)
    assert "Container" in sk.structs
    assert any("Container::add(" in f for f in sk.functions)
    assert any("Container::get(" in f for f in sk.functions)


def test_template_specialization():
    """template<> (explicit specialization) — empty brackets handled."""
    src = """\
template<>
class Container<bool> {
public:
    void setBit(int pos);
};
"""
    sk = extract_skeleton(src)
    # The class name should be captured (Container).
    assert any("Container" in s for s in sk.structs)


# ---------------------------------------------------------------------------
# C/C++ shared features (still work via reused tokenizer)
# ---------------------------------------------------------------------------

def test_includes_and_macros():
    src = """\
#include <iostream>
#include <vector>
#define MAX_SIZE 100
#define MIN(a,b) ((a)<(b)?(a):(b))
int main();
"""
    sk = extract_skeleton(src)
    assert "iostream" in sk.includes
    assert "vector" in sk.includes
    assert "MAX_SIZE" in sk.macros
    assert "MIN(...)" in sk.macros


def test_typedef():
    src = """\
typedef unsigned int uint32;
typedef int (*cmp_fn)(const void *, const void *);
"""
    sk = extract_skeleton(src)
    assert "uint32" in sk.typedefs
    assert "cmp_fn" in sk.typedefs


def test_strings_comments_dont_affect_brace_depth():
    src = """\
int f() {
    // comment with } brace
    char *s = "string with { brace }";
    return 0;
}
int g() { return 1; }
"""
    sk = extract_skeleton(src)
    names = {x.split("(", 1)[0] for x in sk.functions}
    assert "f" in names
    assert "g" in names


def test_extern_c_block():
    src = '''\
#ifndef HEADER_H
#define HEADER_H
#ifdef __cplusplus
extern "C" {
#endif
int api_init(const char *name);
void api_cleanup(void);
#ifdef __cplusplus
}
#endif
#endif
'''
    sk = extract_skeleton(src)
    names = {x.split("(", 1)[0] for x in sk.functions}
    assert "api_init" in names
    assert "api_cleanup" in names


# ---------------------------------------------------------------------------
# Safe degradation
# ---------------------------------------------------------------------------

def test_empty_file():
    sk = extract_skeleton("")
    assert sk.entity_count == 0
    assert sk.render() == ""


def test_malformed_does_not_crash():
    src = "class {{{ ; ; template <<< >>> } } }"
    sk = extract_skeleton(src)
    # Must not crash; result may be partial/empty.
    assert isinstance(sk.structs, list)
    assert isinstance(sk.functions, list)


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

def test_render_dedup():
    src = """\
#include <iostream>
#include <iostream>
class Foo { public: void bar(); };
class Foo { public: void baz(); };
"""
    sk = extract_skeleton(src)
    out = sk.render(max_tokens=400)
    assert out.count("iostream") == 1
    # Foo appears once in Structs; its methods appear as Foo::bar/Foo::baz
    # in Functions (two distinct methods, correctly kept separate).
    assert "Foo" in out


def test_render_truncates_at_token_budget():
    src = "\n".join(f"int func_{i}();" for i in range(200))
    sk = extract_skeleton(src)
    out = sk.render(max_tokens=60)
    assert "File skeleton" in out
    assert len(out) < 60 * 4 + 80
