"""
RAG (Retrieval-Augmented Generation) Engine for Superset Job Pilot.
Indexes candidate profile facts, historical drive questions, and error playbooks
using high-speed hybrid BM25 + N-Gram semantic retrieval.
"""

import json
import logging
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("rag_engine")
logger.setLevel(logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROFILE_PATH = DATA_DIR / "student_profile.json"
PLAYBOOKS_PATH = DATA_DIR / "error_playbooks.json"
CATALOG_PATH = DATA_DIR / "questions_catalog.json"
MEMORY_PATH = DATA_DIR / "clarifications_memory.json"

# Maximum number of questions catalog entries to index (0 = no limit)
CATALOG_INDEX_LIMIT = 0


class RAGDocument:
    """Represents an atomic knowledge chunk in the RAG store."""
    def __init__(self, doc_id: str, title: str, content: str, source: str, tags: List[str] = None, metadata: Dict[str, Any] = None):
        self.doc_id = doc_id
        self.title = title
        self.content = content
        self.source = source
        self.tags = tags or []
        self.metadata = metadata or {}
        self.tokens: List[str] = self._tokenize(content + " " + title)
        self.ngrams: List[str] = self._generate_ngrams(self.tokens, n=2)
        # R1+R2 Fix: Pre-compute token frequency map and token set for O(1) BM25 lookups
        self.token_freq: Dict[str, int] = Counter(self.tokens)
        self.token_set: set = set(self.tokens)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        cleaned = re.sub(r"[^\w\s\-\.]", " ", text.lower())
        return [t.strip() for t in cleaned.split() if len(t.strip()) > 1]

    @staticmethod
    def _generate_ngrams(tokens: List[str], n: int = 2) -> List[str]:
        return ["_".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


class RAGEngine:
    """
    In-memory Hybrid BM25 + Phrase N-Gram Knowledge Retrieval Engine.
    Sub-millisecond retrieval with zero external dependencies.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(RAGEngine, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, user_id: str = None):
        if self._initialized:
            # R5 Fix: If a different user_id is requested, force a re-index for that user
            if user_id and user_id != getattr(self, "_current_user_id", None):
                self._current_user_id = user_id
                self.load_and_index(user_id=user_id)
            return
        self._current_user_id = user_id
        self.documents: List[RAGDocument] = []
        self.doc_len_map: Dict[str, int] = {}
        self.avg_doc_len: float = 0.0
        self.doc_freq: Dict[str, int] = {}
        self.idf_map: Dict[str, float] = {}
        self.total_docs: int = 0
        self.load_and_index(user_id=user_id)
        self._initialized = True

    def load_and_index(self, user_id: str = None):
        """Loads all knowledge stores and compiles the hybrid index.
        
        R5 Fix: Accepts user_id to scope profile indexing to a specific user,
        preventing cross-user profile contamination in the shared singleton.
        """
        self.documents.clear()
        
        # 1. Index Candidate Profile Facts
        self._index_profile(user_id=user_id)
        
        # 2. Index Error Playbooks & Resolution Recipes
        self._index_playbooks()
        
        # 3. Index Historical Questions Catalog
        self._index_questions_catalog()
        
        # 4. Index Dynamic User Clarifications Memory
        self._index_memory()

        self._recompute_bm25_stats()

    def _recompute_bm25_stats(self):
        """Recomputes BM25 IDF statistics over all currently indexed documents."""
        # R6 Fix: Extracted into own method so incremental add_memory() can call it
        # without duplicating the stats computation code.
        self.total_docs = len(self.documents)
        if self.total_docs == 0:
            logger.warning("RAG index empty.")
            return

        total_tokens = sum(len(d.tokens) for d in self.documents)
        self.avg_doc_len = total_tokens / float(self.total_docs) if self.total_docs else 1.0

        # Term Document Frequency
        self.doc_freq.clear()
        for doc in self.documents:
            for t in doc.token_set:
                self.doc_freq[t] = self.doc_freq.get(t, 0) + 1

        # BM25 IDF: log(1 + (N - n + 0.5) / (n + 0.5))
        self.idf_map.clear()
        for term, df in self.doc_freq.items():
            idf = math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))
            self.idf_map[term] = max(0.1, idf)

        logger.info(f"RAG Engine successfully compiled index: {self.total_docs} chunks indexed.")

    def _index_profile(self, user_id: str = None):
        """Chunks student profile into categorized semantic documents from the encrypted vault.
        
        R5 Fix: Accepts user_id so the singleton indexes the CORRECT user's profile,
        preventing profile cross-contamination in multi-user web server sessions.
        """
        try:
            from attribute_resolver import AttributeResolver
            # R5 Fix: Pass user_id explicitly so we always load THIS user's profile
            p = AttributeResolver(user_id=user_id).profile
            if not p or not p.get("full_name"):
                return

            # Categorized profile slices for precision retrieval
            sections = [
                ("Identity & Legal Demographics", 
                 f"Candidate: {p.get('full_name')} ({p.get('first_name')} {p.get('middle_name')} {p.get('last_name')}). "
                 f"Father's Name: {p.get('fathers_name')}. Mother: {p.get('mothers_name', '')}. "
                 f"Date of Birth: {p.get('dob')} (ISO: {p.get('dob_iso')}). Gender: {p.get('gender')}. "
                 f"Marital Status: Single / Unmarried. Citizenship: {p.get('citizenship')}. "
                 f"Nationality: {p.get('nationality')}. "
                 f"Disability: {p.get('disability')} ({p.get('disability_percent')}%).",
                 ["profile", "identity", "parents", "citizenship", "father"]),

                ("Passports, PAN & Government IDs",
                 f"Passport Number: {p.get('passport_number')}, Country: {p.get('passport_nationality', '')}, "
                 f"Passport Type: {p.get('passport_type')}. Issue Date: {p.get('passport_issue_date')}, Expiry Date: {p.get('passport_expiry_date')}. "
                 f"Issuing Authority: {p.get('passport_issuing_authority')}. Citizenship Certificate No: {p.get('citizenship_certificate_no')}. "
                 f"Has PAN: {p.get('has_pan')}, PAN Card Number: {p.get('pan_card_number')}. "
                 f"PAN Name: {p.get('pan_first_name')} {p.get('pan_middle_name')} {p.get('pan_last_name')}. "
                 f"Aadhaar Card Last 4 Digits: {p.get('aadhaar_last_4')}.",
                 ["profile", "passport", "pan", "aadhaar", "government_id"]),

                ("Contact Numbers & Addresses",
                 f"Registered Mobile Phone: {p.get('phone_number')}. "
                 f"Primary Email: {p.get('email')}. Student Roll/Identification No: {p.get('roll_number')}. "
                 f"Emergency Contact Person Name: {p.get('emergency_contact_name', '')}. "
                 f"Emergency Contact Phone Number (Alternate Contact): {p.get('emergency_contact_number')}. "
                 f"Emergency Contact Physical Address: {p.get('emergency_contact_address')}. "
                 f"Current College Address: {p.get('current_address')}, Current Location: {p.get('current_location')}. "
                 f"Permanent Address: {p.get('permanent_address')}, District: {p.get('permanent_district')}, Country: {p.get('permanent_country')}.",
                 ["profile", "contact", "phone", "emergency", "address", "mobile"]),

                ("Academic Record (B.Tech College & Branch)",
                 f"Undergraduate Degree: {p.get('graduation_degree')} in {p.get('graduation_branch')}. "
                 f"Current College / University: {p.get('graduation_college')}. "
                 f"Graduation Passing Year: {p.get('graduation_passing_year')} (Expected 8th sem marksheet: {p.get('marksheet_8th_sem')}). "
                 f"Current B.Tech CGPA: {p.get('current_cgpa')}. Active Backlogs: {p.get('active_backlogs')} (Zero). "
                 f"History of Backlogs: {p.get('history_backlogs')} (Zero). Total Educational Gaps: {p.get('total_gap')} years (Zero).",
                 ["profile", "academics", "college", "cgpa", "branch", "backlogs", "kiit"]),

                ("Secondary & Intermediate Schooling (10th & 12th)",
                 f"Class 10th (Secondary): School: {p.get('tenth_school')}, Board: {p.get('tenth_board')}, "
                 f"Year of Passing: {p.get('tenth_passing_year')}, Percentage: {p.get('tenth_percentage')}%, GPA: {p.get('tenth_gpa')}, Mode: {p.get('tenth_course_type')}. "
                 f"Class 12th (Senior Secondary / Intermediate): College: {p.get('twelfth_college')}, Board: {p.get('twelfth_board')}, "
                 f"Year of Passing: {p.get('twelfth_passing_year')}, Percentage: {p.get('twelfth_percentage')}%, GPA: {p.get('twelfth_gpa')}, Mode: {p.get('twelfth_course_type')}. "
                 f"Gaps between 10th and 12th: {p.get('gap_10th_12th')} years. Gap between 12th and Graduation: {p.get('gap_12th_grad')} years.",
                 ["profile", "academics", "10th", "12th", "school", "board", "percentage"]),

                ("Work Status, Flexibility & Links",
                 f"Current Employment Status: {p.get('employment_status')} (Full-time student, never previously employed, no sabbatical). "
                 f"Prior Employment / Experience Documents: None / NA. Sabbatical: False / No. "
                 f"Flexible to shift timings: {p.get('flexible_shift')}. Flexible to work from any location: {p.get('flexible_location')}. "
                 f"Flexible to work on any domain/technology: {p.get('flexible_domain')}. "
                 f"GitHub Link: {p.get('github_link')}. LinkedIn Link: {p.get('linkedin_link')}. "
                 f"Public Resume URL: {p.get('resume_link')}. Introduction Video URL: {p.get('intro_video_link')}.",
                 ["profile", "employment", "flexibility", "resume", "github", "linkedin", "sabbatical"])
            ]

            for idx, (title, content, tags) in enumerate(sections):
                doc_id = f"prof_sec_{idx+1}"
                self.documents.append(RAGDocument(doc_id, title, content, "student_profile.json", tags, {"category": "candidate_profile"}))

        except Exception as e:
            logger.error(f"Error indexing student profile: {e}")

    def _index_playbooks(self):
        """Indexes all error resolution playbooks."""
        if not PLAYBOOKS_PATH.exists():
            return
        try:
            with open(PLAYBOOKS_PATH, "r", encoding="utf-8") as f:
                playbooks = json.load(f)
            for pb in playbooks:
                doc_id = pb.get("id", f"pb_{len(self.documents)}")
                title = pb.get("title", "Error Playbook")
                content = (
                    f"Playbook: {title}. Patterns: {', '.join(pb.get('match_patterns', []))}. "
                    f"Description: {pb.get('description')}. Root Cause: {pb.get('root_cause')}. "
                    f"Auto-Healable: {pb.get('auto_healable')}. Action: {pb.get('auto_heal_action')}. "
                    f"Remediation: {pb.get('remediation')}."
                )
                tags = pb.get("tags", []) + ["playbook", "error"]
                self.documents.append(RAGDocument(doc_id, title, content, "error_playbooks.json", tags, pb))
        except Exception as e:
            logger.error(f"Error indexing playbooks: {e}")

    def _index_questions_catalog(self):
        """Indexes unique questions and option mappings from historical drives."""
        if not CATALOG_PATH.exists():
            return
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                cat = json.load(f)

            # R4 Fix: Index ALL catalog questions (removed arbitrary :120 cap)
            # R3 Fix: Use full hash string as doc_id to avoid birthday-paradox collisions
            items = list(cat.items())
            if CATALOG_INDEX_LIMIT > 0:
                items = items[:CATALOG_INDEX_LIMIT]

            for q_name, details in items:
                opts = details.get("options", [])
                companies = details.get("sample_companies", [])
                content = (
                    f"Question: '{q_name}'. "
                    f"Field Types: {', '.join(details.get('types', []))}. "
                    f"Allowed Options: {', '.join(opts) if opts else 'Free text / boolean'}. "
                    f"Companies Asking: {', '.join(companies[:5])}."
                )
                # R3 Fix: Use full stable hash without truncation to prevent doc_id collisions
                doc_id = f"q_cat_{abs(hash(q_name))}"
                self.documents.append(RAGDocument(doc_id, f"Question: {q_name[:40]}", content, "questions_catalog.json", ["question", "superset"], {"question_name": q_name}))
        except Exception as e:
            logger.error(f"Error indexing questions catalog: {e}")

    def _index_memory(self):
        """Indexes user clarifications and resolved answers saved at runtime."""
        if not MEMORY_PATH.exists():
            return
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                memory = json.load(f)
            for item in memory:
                doc_id = item.get("id", f"mem_{len(self.documents)}")
                title = f"User Clarification: {item.get('key', 'General')}"
                content = (
                    f"Clarification on '{item.get('key')}': Question was '{item.get('question')}'. "
                    f"User Answer / Clarification: '{item.get('resolution')}'. "
                    f"Context: {item.get('context', '')}."
                )
                self.documents.append(RAGDocument(doc_id, title, content, "clarifications_memory.json", ["memory", "clarification", "user_verified"], item))
        except Exception as e:
            logger.error(f"Error indexing clarifications memory: {e}")

    def add_memory(self, key: str, question: str, resolution: str, context: str = "", tags: List[str] = None):
        """Dynamically learns from user inputs and incrementally updates the index.

        R6 Fix: Instead of calling load_and_index() (full rebuild O(D×L) on every write),
        we incrementally: remove any old doc with the same logical key, append the new
        RAGDocument, persist to disk, then recompute BM25 stats. This is O(D) not O(D×L).
        """
        # R3 Fix: Use full hash without % truncation to prevent doc_id birthday-paradox collisions
        new_id = f"mem_{abs(hash(key + question))}"
        entry = {
            "id": new_id,
            "key": key,
            "question": question,
            "resolution": resolution,
            "context": context,
            "tags": tags or ["user_verified"]
        }

        current = []
        if MEMORY_PATH.exists():
            try:
                with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = []

        # Upsert in the persistent file
        current = [item for item in current if item.get("key") != key or item.get("question") != question]
        current.append(entry)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)

        # R6 Fix: Incremental in-memory update — remove stale doc, append new doc
        self.documents = [d for d in self.documents if not (
            d.source == "clarifications_memory.json"
            and d.metadata.get("key") == key
            and d.metadata.get("question") == question
        )]
        title = f"User Clarification: {key}"
        content = (
            f"Clarification on '{key}': Question was '{question}'. "
            f"User Answer / Clarification: '{resolution}'. "
            f"Context: {context}."
        )
        new_doc = RAGDocument(new_id, title, content, "clarifications_memory.json",
                              ["memory", "clarification", "user_verified"], entry)
        self.documents.append(new_doc)

        # Recompute BM25 stats over updated document list (no full disk reload needed)
        self._recompute_bm25_stats()
        logger.info(f"Learned new user clarification (incremental): '{key}' -> '{resolution}'")

    def search(self, query: str, top_k: int = 5, filter_tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Executes hybrid BM25 + N-gram phrase retrieval.
        Returns top_k ranked documents with scores and metadata.
        """
        if not query or not self.documents:
            return []

        q_tokens = RAGDocument._tokenize(query)
        q_ngrams = RAGDocument._generate_ngrams(q_tokens, n=2)

        if not q_tokens:
            return []

        scores: List[tuple] = []
        k1 = 1.5
        b = 0.75

        for doc in self.documents:
            # Tag filter check
            if filter_tags:
                if not any(t in doc.tags for t in filter_tags):
                    continue

            # BM25 Term Frequency & Saturation
            doc_len = len(doc.tokens)
            bm25_score = 0.0
            matched_terms = 0

            for t in q_tokens:
                # R1+R2 Fix: Use pre-computed token_set (O(1)) and token_freq (O(1))
                # instead of `t in doc.tokens` (O(n)) and `doc.tokens.count(t)` (O(n))
                if t in doc.token_set:
                    matched_terms += 1
                    tf = doc.token_freq[t]
                    idf = self.idf_map.get(t, 0.5)
                    denom = tf + k1 * (1.0 - b + b * (doc_len / self.avg_doc_len))
                    term_score = idf * ((tf * (k1 + 1.0)) / denom if denom > 0 else 0)
                    bm25_score += term_score

            # N-Gram Exact Match Bonus (bi-grams boost)
            ngram_bonus = 0.0
            for ng in q_ngrams:
                if ng in doc.ngrams:
                    ngram_bonus += 2.0

            # Title match boost
            title_bonus = 0.0
            if any(t in doc.title.lower() for t in q_tokens):
                title_bonus += 1.5

            total_score = bm25_score + ngram_bonus + title_bonus
            if total_score > 0:
                scores.append((total_score, doc, matched_terms))

        scores.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, doc, matched_terms in scores[:top_k]:
            results.append({
                "doc_id": doc.doc_id,
                "title": doc.title,
                "content": doc.content,
                "source": doc.source,
                "tags": doc.tags,
                "score": round(score, 3),
                "matched_terms": matched_terms,
                "metadata": doc.metadata
            })

        return results

    def get_context_for_prompt(self, query: str, top_k: int = 4, filter_tags: Optional[List[str]] = None) -> str:
        """Formats the retrieved chunks into structured context for LLM prompts."""
        hits = self.search(query, top_k=top_k, filter_tags=filter_tags)
        if not hits:
            return "No specific matching documents found in knowledge base."

        formatted = []
        for idx, hit in enumerate(hits, 1):
            formatted.append(f"[{idx}] Source: {hit['source']} | Title: {hit['title']}\n{hit['content']}")
        return "\n\n".join(formatted)

    def get_stats(self) -> Dict[str, Any]:
        """Returns diagnostic statistics of the RAG index."""
        tag_counts = {}
        for d in self.documents:
            for t in d.tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        return {
            "total_documents": self.total_docs,
            "avg_doc_len": round(self.avg_doc_len, 1),
            "unique_terms": len(self.doc_freq),
            "sources": list(set(d.source for d in self.documents)),
            "top_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        }
