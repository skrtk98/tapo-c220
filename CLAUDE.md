# このプロジェクトの共通ルール

# Git運用ルール
- 実装が一区切りついたら、必ず `git add` と `git commit` を実行してください。
- コミットは変更内容ごとに行ってください。
- コミットメッセージは変更内容を簡潔に要約した1行（またはConventional Commits形式）で作成してください。
- コミット前に必ずテストや検証コマンドを実行してください。
- プッシュ時にdeployを実行してください。

## Codex 連携ルール (codex-plugin-cc)
- コードの実装や大きな変更を行った後は、必ず Codex によるセカンドオピニオンやレビューを取得すること。
- `/codex:review` と `/codex:adversarial-review` は `disable-model-invocation: true` のためユーザー専用で、Claude からは起動できない。
  Claude がレビューを取得するときは、ルーター経由で次のスキルを使う：
    - `hybrid-router:review --backend codex` : 現在の変更差分のコードレビューを行う
    - `hybrid-router:adversarial-review --backend codex [観点]` : 設計や実装の批判的・敵対的検証を行う
    - `codex:rescue` : バグ調査や修正をCodexに委任する（直接起動できる）
- ルーターは既定で ollama を選ぶ。このプロジェクトでは codex を使うため、`--backend codex` を必ず付ける。
- 通常レビューは組み込みレビューアに直結しており、観点を指定する文言を受け付けない。
  観点を渡したいときは adversarial-review を使う。
- 既にコミット済みの変更をレビューさせるときは `--base HEAD~1` のように基準を指定する。
- レビュー結果を報告するときは、ユーザーが自分で `/codex:review` や `/codex:adversarial-review` を実行して再確認できることを一言添える。
