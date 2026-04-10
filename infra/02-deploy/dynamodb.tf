resource "aws_dynamodb_table" "orders" {
  name         = "${local.prefix}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "order_num"
    type = "S"
  }

  global_secondary_index {
    name            = "run_id-order_num-index"
    hash_key        = "run_id"
    range_key       = "order_num"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "order_events" {
  name         = "${local.prefix}-order-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "trace_id"
  range_key    = "sk"

  attribute {
    name = "trace_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "order_name"
    type = "S"
  }

  attribute {
    name = "epoch"
    type = "N"
  }

  global_secondary_index {
    name            = "order_name_index"
    hash_key        = "order_name"
    range_key       = "epoch"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "orchestrator_locks" {
  name         = "${local.prefix}-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
