# Git LFS 安全ルール

Git LFS ファイルの操作に関する安全ルール。LFS push エラー時の対応フローと `GIT_LFS_SKIP_PUSH` の使用条件を定義する。

## GIT_LFS_SKIP_PUSH の使用条件

`GIT_LFS_SKIP_PUSH=1` は Git コミット部分のみを push し、LFS オブジェクトの転送をスキップする。
**以下の条件を両方満たす場合のみ使用可**。

### 使用可能条件

1. **対象 LFS オブジェクトが既にサーバーに存在すること**（新規 LFS ファイルには使用禁止）
2. **LFS push 自体が一時的な 502/504 ネットワークエラーで失敗していること**

```bash
# 条件1の確認: LFS オブジェクトがリモートに存在するか確認
# 出力が空（何も表示されない）→ 全オブジェクトがリモートに存在済み → GIT_LFS_SKIP_PUSH=1 使用可
# 出力あり → 新規オブジェクトが含まれる → GIT_LFS_SKIP_PUSH=1 使用禁止
git lfs push --dry-run origin {ブランチ名} 2>&1 | grep "Uploading LFS objects" | grep -v "(0/0)"
```

### 絶対禁止

- **新規 LFS ファイルに `GIT_LFS_SKIP_PUSH=1` を使ってはいけない**
  → LFS オブジェクトが消失し、他の環境で `git checkout` した際に壊れたポインタファイルが展開される
- `--force` と `GIT_LFS_SKIP_PUSH=1` の組み合わせ

### 使用例（502 エラーでの一時回避）

```bash
# 既存 LFS ファイルのみの場合（新規 LFS ファイルがないことを確認してから）
GIT_LFS_SKIP_PUSH=1 git push origin {ブランチ名}
```

---

## LFS push 502 エラー発生時の診断フロー

```
git push が 502 エラーで失敗
  ↓
1. 新規 LFS ファイルがあるか確認
   git lfs status  # 新規ステージ済み LFS ファイルを確認
  ↓
2a. 新規 LFS ファイルがある場合:
    → リトライ（指数バックオフ: 30秒→60秒→120秒、最大3回）
    → 3回失敗 → ユーザーに報告して判断を仰ぐ
  ↓
2b. 新規 LFS ファイルがない場合（既存オブジェクトのみ）:
    → GIT_LFS_SKIP_PUSH=1 git push でコミット部分だけ push
    → LFS オブジェクトは後で git lfs push --all で再送
```

---

## LFS ファイル完全性チェック（パイプライン共通）

各パイプラインの Step 0 で以下を実行し、LFS ポインタが実際のバイナリとして展開済みか確認する。

```bash
# LFS ファイルが実バイナリとして存在するか確認（ポインタ検出）
lfs_broken=0
while IFS= read -r line; do
  path=$(echo "$line" | awk '{print $3}')
  if [ -f "$path" ]; then
    # LFS ポインタは "version https://git-lfs..." で始まる小さいファイル
    size=$(wc -c < "$path")
    if [ "$size" -lt 200 ] && head -1 "$path" 2>/dev/null | grep -q "^version https://git-lfs"; then
      echo "WARNING: LFS pointer not expanded: $path"
      lfs_broken=1
    fi
  fi
done < <(git lfs ls-files 2>/dev/null)

if [ "$lfs_broken" -eq 1 ]; then
  echo "LFS ファイルが未展開です。git lfs pull を実行してください。"
  exit 1
fi
```

> **適用パイプライン**: image-pipeline, video-pipeline, audio-pipeline, shorts-pipeline の Step 0

---

## 関連 Issue

- #589: [Retro][shorts] LFS ファイル完全性チェックをパイプライン品質ゲートに追加
- #752: [Retro][image] GIT_LFS_SKIP_PUSH の使用条件を文書化し LFS オブジェクト既存性チェックを自動化する
