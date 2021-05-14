import React, { useState } from 'react'
import { DashboardOutlined } from '@ant-design/icons'
import { router } from 'kea-router'

export function InsightTitle(): JSX.Element {
    const [{ fromItemName, fromDashboard }] = useState(router.values.hashParams)
    return (
        <>
            <h3 className="l3" style={{ display: 'flex', alignItems: 'center' }}>
                {fromDashboard && (
                    <DashboardOutlined
                        style={{ color: 'var(--warning)', marginRight: 4 }}
                        title="Insight saved on dashboard"
                    />
                )}
                {fromItemName || 'Unsaved query'}
            </h3>
        </>
    )
}