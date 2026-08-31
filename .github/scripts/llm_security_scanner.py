"""LLM-based code security scanner (CI helper, not part of the app).

Reworked from the uploaded template, whose failure modes all pointed the
same direction — looking clean while finding nothing:

- The LLM was told "provide only the JSON output", but gpt-4o routinely
  wraps its answer in a ```json fence anyway. json.loads() then failed, the
  except-block logged the whole response (findings included) into the CI
  log, returned [], and the report said "Congratulations! No security
  vulnerabilities were detected." An hour of paid scanning, discarded.
  Fences are stripped now, and a response that still doesn't parse is an
  ERROR on that file, never a clean bill.
- Every API failure (bad key, retired model, rate limit) was swallowed into
  "no vulnerabilities". Failures now mark the file as errored, errors are
  listed in the report, and the process exits non-zero when every file
  errored — a dead key turns the CI step red instead of green.
- Filenames are untrusted input on a public repo. --file-list reads a NUL-
  or newline-delimited file so no shell ever interprets them.
"""

import os
import sys
import argparse
import json
import re
from pathlib import Path
import openai
from typing import List, Dict, Any, Optional
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('llm-security-scanner')

SUPPORTED_EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.ts': 'typescript',
    '.java': 'java',
    '.c': 'c',
    '.cpp': 'c++',
    '.go': 'go',
    '.php': 'php',
    '.rb': 'ruby',
}

DEFAULT_EXCLUDE_DIRS = [".git", "node_modules", "venv", "__pycache__", ".env",
                        "scan-results"]


class ScanError(Exception):
    """An analysis that did not produce a trustworthy answer.

    Deliberately distinct from "no vulnerabilities": the template collapsed
    the two, so an invalid API key produced a spotless report.
    """


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    """Parse the LLM's vulnerability list out of its response text.

    Models wrap JSON in markdown fences no matter how firmly the prompt
    forbids it, and some return {"vulnerabilities": [...]} instead of a bare
    array. Handle both; raise ScanError when nothing parseable remains.
    """
    t = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fenced:
        t = fenced.group(1).strip()
    if not t.startswith('[') and not t.startswith('{'):
        start, end = t.find('['), t.rfind(']')
        if start != -1 and end > start:
            t = t[start:end + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise ScanError(f"response was not parseable JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("vulnerabilities", [data])
    if not isinstance(data, list):
        raise ScanError(f"expected a JSON array, got {type(data).__name__}")
    return [v for v in data if isinstance(v, dict)]


class CodeSecurityScanner:
    """A security scanner that uses LLMs to detect vulnerabilities in code."""

    def __init__(self, api_key: str, model: str = "gpt-4o", provider: str = "openai"):
        self.provider = provider
        self.model = model

        if provider == "openai":
            self.client = openai.OpenAI(api_key=api_key)
        elif provider == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.info(f"Initialized {provider} client with model {model}")

    def scan_file(self, file_path: str) -> Dict[str, Any]:
        """Scan a single file. status is 'completed', 'skipped', or 'error' —
        and 'error' is never reported as a clean file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return {"file": file_path, "status": "error", "error": str(e)}

        language = SUPPORTED_EXTENSIONS.get(Path(file_path).suffix.lower())
        if not language:
            logger.info(f"Unsupported file type — skipping {file_path}")
            return {"file": file_path, "status": "skipped",
                    "reason": "unsupported_file_type"}

        try:
            vulnerabilities = self._analyze_code(code, language)
        except ScanError as e:
            logger.error(f"Scan failed for {file_path}: {e}")
            return {"file": file_path, "status": "error", "error": str(e)}

        return {
            "file": file_path,
            "status": "completed",
            "language": language,
            "vulnerabilities": vulnerabilities,
        }

    def scan_files(self, paths: List[str]) -> List[Dict[str, Any]]:
        results = []
        for p in paths:
            logger.info(f"Scanning file: {p}")
            results.append(self.scan_file(p))
        return results

    def scan_directory(self, directory_path: str, recursive: bool = True,
                       exclude_dirs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if exclude_dirs is None:
            exclude_dirs = list(DEFAULT_EXCLUDE_DIRS)

        walk_dir = Path(directory_path)
        logger.info(f"Scanning directory: {walk_dir}")
        return self.scan_files(
            [str(p) for p in self._get_files_to_scan(walk_dir, recursive, exclude_dirs)])

    def _get_files_to_scan(self, directory: Path, recursive: bool,
                           exclude_dirs: List[str]) -> List[Path]:
        globber = directory.rglob('*') if recursive else directory.glob('*')
        return sorted(p for p in globber if self._should_scan_file(p, exclude_dirs))

    def _should_scan_file(self, path: Path, exclude_dirs: List[str]) -> bool:
        if path.is_dir():
            return False
        for parent in path.parents:
            if parent.name in exclude_dirs:
                return False
        return path.suffix.lower() in SUPPORTED_EXTENSIONS

    def _analyze_code(self, code: str, language: str) -> List[Dict[str, Any]]:
        """Ask the LLM. Raises ScanError on any failure — an unanswered
        question is not the same thing as 'no vulnerabilities'."""
        prompt = self._build_security_prompt(code, language)
        if self.provider == "openai":
            return self._analyze_with_openai(prompt)
        return self._analyze_with_anthropic(prompt)

    def _build_security_prompt(self, code: str, language: str) -> str:
        return f"""
        You are a cybersecurity expert specializing in secure coding practices and vulnerability detection.

        Analyze the following {language} code for security vulnerabilities, focusing on:

        1. Common vulnerabilities specific to {language}
        2. Injection vulnerabilities (SQL, command, etc.)
        3. Authentication and authorization issues
        4. Data validation and sanitization problems
        5. Cryptographic flaws
        6. Hardcoded credentials or secrets
        7. Insecure configurations
        8. Race conditions or concurrency issues
        9. Error handling that leaks sensitive information
        10. Any other security concerns

        For each vulnerability found, provide:
        1. A brief description of the vulnerability
        2. The severity level (exactly one of: Critical, High, Medium, Low, Info)
        3. The specific line number(s) where the issue occurs
        4. The potential impact of exploiting the vulnerability
        5. A recommended fix with code example

        Format your response as a JSON array of objects, each representing a vulnerability, with the following structure:

        [
            {{
                "vulnerability_type": "Type of vulnerability",
                "description": "Brief description",
                "severity": "Severity level",
                "line_numbers": [line numbers],
                "impact": "Potential impact",
                "recommendation": "Recommended fix",
                "fix_example": "Code example"
            }}
        ]

        If no vulnerabilities are found, return an empty array: []

        Here is the code to analyze:

        ```{language}
        {code}
        ```

        Provide only the JSON output without any additional text.
        """

    def _analyze_with_openai(self, prompt: str) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        # The gpt-5 / o-series reasoning models REJECT a temperature parameter
        # ("unsupported parameter"); only the gpt-4 family accepts it.
        if self.model.startswith("gpt-4"):
            kwargs["temperature"] = 0.0
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a cybersecurity expert that analyzes code for security vulnerabilities."},
                    {"role": "user", "content": prompt},
                ],
                **kwargs,
            )
        except Exception as e:
            time.sleep(2)  # ease off in case this was a rate limit
            raise ScanError(f"OpenAI API call failed: {e}") from e
        return extract_json_array(response.choices[0].message.content)

    def _analyze_with_anthropic(self, prompt: str) -> List[Dict[str, Any]]:
        # No temperature: current Claude models reject sampling parameters.
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            time.sleep(2)
            raise ScanError(f"Anthropic API call failed: {e}") from e
        text = "".join(b.text for b in response.content if b.type == "text")
        return extract_json_array(text)


def summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "total_files_scanned": len(results),
        "vulnerable_files": sum(1 for r in results
                                if r.get('status') == 'completed' and r.get('vulnerabilities')),
        "total_vulnerabilities": sum(len(r.get('vulnerabilities') or [])
                                     for r in results if r.get('status') == 'completed'),
        "error_files": sum(1 for r in results if r.get('status') == 'error'),
        "skipped_files": sum(1 for r in results if r.get('status') == 'skipped'),
    }


def generate_report(results: List[Dict[str, Any]], output_format: str = 'json',
                    output_file: Optional[str] = None) -> None:
    summary = summarize(results)

    if output_format == 'json':
        write_json_report(results, output_file)

    elif output_format == 'markdown':
        markdown = "# Security Scan Report\n\n"
        markdown += "## Summary\n\n"
        markdown += f"- Total Files Scanned: {summary['total_files_scanned']}\n"
        markdown += f"- Files with Vulnerabilities: {summary['vulnerable_files']}\n"
        markdown += f"- Total Vulnerabilities Found: {summary['total_vulnerabilities']}\n"
        markdown += f"- Files with Scan Errors: {summary['error_files']}\n\n"

        if summary['total_vulnerabilities'] > 0:
            markdown += "## Vulnerabilities\n\n"
            for result in results:
                if result.get('status') == 'completed' and result.get('vulnerabilities'):
                    markdown += f"### {result['file']}\n\n"
                    for vuln in result['vulnerabilities']:
                        markdown += f"#### {vuln.get('vulnerability_type', 'Unknown Vulnerability')}\n\n"
                        markdown += f"- **Severity**: {vuln.get('severity', 'Unknown')}\n"
                        lines = vuln.get('line_numbers', [])
                        if not isinstance(lines, list):
                            lines = [lines]
                        markdown += f"- **Line Numbers**: {', '.join(map(str, lines))}\n"
                        markdown += f"- **Description**: {vuln.get('description', 'No description provided')}\n"
                        markdown += f"- **Impact**: {vuln.get('impact', 'Unknown impact')}\n"
                        markdown += f"- **Recommendation**: {vuln.get('recommendation', 'No recommendation provided')}\n"
                        if vuln.get('fix_example'):
                            markdown += f"\n**Fix Example**:\n\n```\n{vuln['fix_example']}\n```\n\n"

        # The template hid errors and congratulated the operator on a scan
        # that had silently analyzed nothing. Errors are report content.
        errored = [r for r in results if r.get('status') == 'error']
        if errored:
            markdown += "## Scan Errors\n\n"
            markdown += "These files were NOT analyzed — do not read their absence as a clean result.\n\n"
            for r in errored:
                markdown += f"- `{r['file']}`: {r.get('error', 'unknown error')}\n"
            markdown += "\n"

        if summary['total_vulnerabilities'] == 0 and not errored:
            markdown += "## No Vulnerabilities Found\n\n"
            markdown += "No security vulnerabilities were detected in the scanned files.\n"

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(markdown)
        else:
            print(markdown)

    else:
        raise ValueError(f"Unsupported output format: {output_format}")


def write_json_report(results: List[Dict[str, Any]], output_file: Optional[str]) -> None:
    report = {"summary": summarize(results), "results": results}
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
    else:
        print(json.dumps(report, indent=2))


def read_file_list(path: str, max_files: int = 0) -> List[str]:
    """Read a NUL- or newline-delimited list of paths (git diff -z output).

    NUL-delimited is the safe transport for untrusted filenames — nothing
    shell-like ever gets a chance to interpret them.
    """
    raw = Path(path).read_bytes()
    sep = b'\x00' if b'\x00' in raw else b'\n'
    entries = [e.decode('utf-8', errors='surrogateescape').strip('\r')
               for e in raw.split(sep)]
    entries = [e for e in entries if e]
    if max_files and len(entries) > max_files:
        logger.warning(f"{len(entries)} files listed, scanning the first "
                       f"{max_files} — the rest are NOT covered by this run.")
        entries = entries[:max_files]
    return entries


def main():
    parser = argparse.ArgumentParser(description='LLM-based Code Security Scanner')

    api_group = parser.add_argument_group('API Configuration')
    api_group.add_argument('--provider', choices=['openai', 'anthropic'], default='openai',
                           help='LLM provider (default: openai)')
    api_group.add_argument('--api-key', help='API key (or OPENAI_API_KEY / ANTHROPIC_API_KEY env var)')
    api_group.add_argument('--model', help='Model to use (default depends on provider)')

    scan_group = parser.add_argument_group('Scanning Options')
    scan_group.add_argument('--file', help='Scan a single file')
    scan_group.add_argument('--file-list',
                            help='Scan the files listed in this NUL- or newline-delimited file')
    scan_group.add_argument('--directory', help='Scan a directory')
    scan_group.add_argument('--max-files', type=int, default=0,
                            help='Cap the number of files taken from --file-list (0 = no cap)')
    scan_group.add_argument('--recursive', action='store_true', default=True,
                            help='Recursively scan directories (default: True)')
    scan_group.add_argument('--exclude-dirs', nargs='+', default=list(DEFAULT_EXCLUDE_DIRS),
                            help='Directory names to exclude from scanning')

    output_group = parser.add_argument_group('Output Options')
    output_group.add_argument('--output-format', choices=['json', 'markdown'], default='json',
                              help='Output format (default: json)')
    output_group.add_argument('--output-file', help='Output file path')
    output_group.add_argument('--json-output-file',
                              help='Additionally write the JSON report here '
                                   '(machine-readable, independent of --output-format)')

    args = parser.parse_args()

    sources = [s for s in (args.file, args.file_list, args.directory) if s]
    if len(sources) != 1:
        parser.error('Exactly one of --file, --file-list or --directory must be specified')

    api_key = args.api_key
    if not api_key:
        env_var = 'OPENAI_API_KEY' if args.provider == 'openai' else 'ANTHROPIC_API_KEY'
        api_key = os.getenv(env_var)
    if not api_key:
        parser.error(f'{args.provider.upper()}_API_KEY environment variable or --api-key must be set')

    model = args.model
    if not model:
        model = 'gpt-5.6-terra' if args.provider == 'openai' else 'claude-opus-5'

    scanner = CodeSecurityScanner(api_key=api_key, model=model, provider=args.provider)

    if args.file:
        results = [scanner.scan_file(args.file)]
    elif args.file_list:
        results = scanner.scan_files(read_file_list(args.file_list, args.max_files))
    else:
        results = scanner.scan_directory(
            args.directory, recursive=args.recursive, exclude_dirs=args.exclude_dirs)

    generate_report(results, args.output_format, args.output_file)
    if args.json_output_file:
        write_json_report(results, args.json_output_file)

    summary = summarize(results)
    scannable = summary['total_files_scanned'] - summary['skipped_files']
    logger.info(f"Done: {summary}")
    if scannable > 0 and summary['error_files'] == scannable:
        # Every single file errored — a dead key or retired model, not a
        # clean codebase. Fail loudly so CI shows red, not a green nothing.
        logger.error("Every file errored; no analysis was performed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
